# FILE: docker/websocket_server/app.py
# Version: 4.1.0 - Major Stability & Performance Update
# Changes v4.1.0:
#   - Circuit Breaker pattern for ASR/TTS/LLM services
#   - TTS response caching for common phrases
#   - Connection watchdog monitoring
#   - Multi-language PIN recognition (15+ languages)
#   - Multi-language sentiment analysis with churn detection
#   - Health check endpoint for service monitoring
#   - LOCALE_MAP moved to global constant (performance)
#   - Enhanced error handling and recovery
# Previous (v3.0.0):
#   - Multi-language support (responses match TTS language setting)
#   - Completely redesigned conversation prompts for natural speech
#   - New upgrade request flow with full database tracking
#   - Intelligent multi-state conversation handling
#   - Performance: Connection pooling, query caching, HTTP/2
#   - Auto-initialization of upgrade_requests table
#   - Session cleanup and memory management
#   - Retry logic with exponential backoff

import asyncio
import os
import io
import wave
import tempfile
import traceback
import websockets
import time
import logging
import json
import subprocess
import httpx
import re
import signal
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional, Any
from collections import OrderedDict, defaultdict

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# PostgreSQL import
try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    logging.warning("asyncpg not installed. Database features disabled.")
    ASYNCPG_AVAILABLE = False

# =============================================================================
# GLOBAL CONNECTION POOLS (Performance improvement)
# =============================================================================
_http_client: Optional[httpx.AsyncClient] = None
_http_client_lock = asyncio.Lock()

async def get_http_client() -> httpx.AsyncClient:
    """Get or create shared HTTP client with connection pooling - thread safe"""
    global _http_client
    
    # Fast path - client exists and is healthy
    if _http_client is not None and not _http_client.is_closed:
        return _http_client
    
    # Slow path - need to create client (with lock to prevent race condition)
    async with _http_client_lock:
        # Double check after acquiring lock
        if _http_client is not None and not _http_client.is_closed:
            return _http_client
            
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            http2=True
        )
        logging.info("[HTTP] Created new HTTP client pool")
    
    return _http_client

async def close_http_client():
    """Close the shared HTTP client"""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
        logging.info("[HTTP] HTTP client pool closed")


# =============================================================================
# RATE LIMITER - Token Bucket Algorithm
# =============================================================================
class RateLimiter:
    """Token bucket rate limiter"""
    
    def __init__(self, requests_per_minute: int = 30):
        self._buckets = defaultdict(lambda: {"tokens": requests_per_minute, "last": time.time()})
        self._rate = requests_per_minute
    
    def is_allowed(self, client_id: str) -> bool:
        bucket = self._buckets[client_id]
        now = time.time()
        
        # Refill tokens
        elapsed = now - bucket["last"]
        bucket["tokens"] = min(self._rate, bucket["tokens"] + elapsed * (self._rate / 60))
        bucket["last"] = now
        
        # Check if request allowed
        if bucket["tokens"] >= 1:
            bucket["tokens"] -= 1
            return True
        return False
    
    def cleanup(self):
        """Remove old entries"""
        now = time.time()
        old = [k for k, v in self._buckets.items() if now - v["last"] > 300]
        for k in old:
            del self._buckets[k]

_rate_limiter = RateLimiter(requests_per_minute=30)


# =============================================================================
# REQUEST CACHE - For common database queries
# =============================================================================
class SimpleCache:
    """Simple in-memory cache with TTL"""
    
    def __init__(self, default_ttl: int = 60):
        self._cache = {}
        self._ttl = default_ttl
    
    def get(self, key: str):
        """Get value from cache if not expired"""
        if key in self._cache:
            value, expiry = self._cache[key]
            if time.time() < expiry:
                return value
            else:
                del self._cache[key]
        return None
    
    def set(self, key: str, value, ttl: int = None):
        """Set value in cache with TTL"""
        expiry = time.time() + (ttl or self._ttl)
        self._cache[key] = (value, expiry)
    
    def clear(self):
        """Clear all cache entries"""
        self._cache.clear()
    
    def cleanup(self):
        """Remove expired entries"""
        now = time.time()
        expired = [k for k, (v, exp) in self._cache.items() if now >= exp]
        for k in expired:
            del self._cache[k]

# Global cache instance
_query_cache = SimpleCache(default_ttl=30)  # 30 second TTL for DB queries

# Database connection pool
_db_pool: Any = None
_db_pool_lock = asyncio.Lock()
_db_settings: dict = {}  # Store last used db settings for refresh

async def get_db_pool(host, port, dbname, user, password) -> Any:
    """Get or create database connection pool"""
    global _db_pool, _db_settings
    
    # Fast check
    if _db_pool:
        return _db_pool

    # Store settings for later use (refresh requests)
    _db_settings = {
        'host': host,
        'port': port,
        'database': dbname,
        'user': user,
        'password': password
    }
    
    async with _db_pool_lock:
        if _db_pool is None and ASYNCPG_AVAILABLE:
            try:
                _db_pool = await asyncpg.create_pool(
                    min_size=1,
                    max_size=10,
                    host=host,
                    port=port,
                    database=dbname,
                    user=user,
                    password=password
                )
                logging.info(f"Database pool created: {dbname}@{host}")
            except Exception as e:
                logging.error(f"Failed to create DB pool: {e}")
                return None
    return _db_pool

async def ensure_db_pool() -> Any:
    """Ensure db pool exists, using stored settings if available"""
    global _db_pool, _db_settings
    if _db_pool:
        return _db_pool
    if _db_settings:
        return await get_db_pool(**_db_settings)
    return None

async def close_db_pool():
    """Close database connection pool"""
    global _db_pool
    if _db_pool:
        await _db_pool.close()
        _db_pool = None
        logging.info("Database pool closed")


# --- Default Settings from Environment Variables ---
def get_bool_env(var_name: str, default: bool = False) -> bool:
    return os.getenv(var_name, str(default)).lower() in ('true', '1', 't', 'yes', 'y')

DEFAULT_SETTINGS = {
    "llm_api_base": os.getenv("LLM_API_BASE"),
    "llm_api_key": os.getenv("LLM_API_KEY"),
    "llm_prompt_template": os.getenv("LLM_PROMPT_TEMPLATE", 'Answer the question: "{transcript}"\n\nAnswer concisely.'),
    "llm_model_name": os.getenv("LLM_MODEL_NAME", "meta-llama/Llama-3.2-1B-Instruct"),
    # ASR - Whisper
    "asr_server_address": os.getenv("ASR_SERVER_ADDRESS"),
    "asr_language_code": os.getenv("ASR_LANGUAGE_CODE", "en"),
    # TTS - XTTS v2
    "tts_server_address": os.getenv("TTS_SERVER_ADDRESS", "localhost:8000"),
    "tts_language_code": os.getenv("TTS_LANGUAGE_CODE", "en"),
    "tts_speaker": os.getenv("TTS_SPEAKER", ""),
    "tts_sample_rate_hz": int(os.getenv("TTS_SAMPLE_RATE_HZ", "24000")),
    # Database settings
    "db_enabled": get_bool_env("DB_ENABLED", False),
    "db_host": os.getenv("DB_HOST", "localhost"),
    "db_port": os.getenv("DB_PORT", "5432"),
    "db_name": os.getenv("DB_NAME", "customer_service"),
    "db_user": os.getenv("DB_USER", "agent"),
    "db_password": os.getenv("DB_PASSWORD", ""),
}

SENTENCE_TERMINATORS = ['.', '?', '!']

# =============================================================================
# LOCALE MAPPING - For ASR language codes (moved outside function for performance)
# =============================================================================
LOCALE_MAP = {
    'en': 'en-US', 'he': 'he-IL', 'es': 'es-ES', 'fr': 'fr-FR',
    'de': 'de-DE', 'it': 'it-IT', 'pt': 'pt-PT', 'pl': 'pl-PL',
    'tr': 'tr-TR', 'ru': 'ru-RU', 'nl': 'nl-NL', 'cs': 'cs-CZ',
    'ar': 'ar-SA', 'zh': 'zh-CN', 'ja': 'ja-JP', 'hu': 'hu-HU',
    'ko': 'ko-KR', 'hi': 'hi-IN', 'vi': 'vi-VN', 'th': 'th-TH',
    'uk': 'uk-UA', 'el': 'el-GR', 'ro': 'ro-RO', 'sv': 'sv-SE',
    'da': 'da-DK', 'fi': 'fi-FI', 'no': 'no-NO',
}

# =============================================================================
# CIRCUIT BREAKER - Prevents cascading failures when services are down
# =============================================================================
class CircuitBreaker:
    """Circuit breaker pattern to prevent overwhelming failed services"""
    
    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: int = 30):
        self.name = name
        self.failures = 0
        self.threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED (normal), OPEN (blocking), HALF_OPEN (testing)
    
    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.threshold:
            self.state = "OPEN"
            logging.warning(f"[CircuitBreaker:{self.name}] OPENED after {self.failures} failures")
    
    def record_success(self):
        if self.state == "HALF_OPEN":
            logging.info(f"[CircuitBreaker:{self.name}] Recovery successful, closing circuit")
        self.failures = 0
        self.state = "CLOSED"
    
    def can_execute(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
                logging.info(f"[CircuitBreaker:{self.name}] Entering HALF_OPEN state, testing...")
                return True
            return False
        return True  # HALF_OPEN allows one request through
    
    def get_status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "failures": self.failures,
            "threshold": self.threshold,
        }

# Global circuit breakers for each service
_asr_circuit = CircuitBreaker("ASR", failure_threshold=5, recovery_timeout=30)
_tts_circuit = CircuitBreaker("TTS", failure_threshold=5, recovery_timeout=30)
_llm_circuit = CircuitBreaker("LLM", failure_threshold=3, recovery_timeout=60)

# =============================================================================
# MULTI-LANGUAGE NUMBER WORDS - For PIN recognition in all supported languages
# =============================================================================
NUMBER_WORDS = {
    'en': {
        'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
        'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
        'oh': '0', 'o': '0',
    },
    'he': {
        'אפס': '0', 'אחת': '1', 'אחד': '1', 'שתיים': '2', 'שניים': '2', 'שתים': '2',
        'שלוש': '3', 'שלושה': '3', 'ארבע': '4', 'ארבעה': '4', 'חמש': '5', 'חמישה': '5',
        'שש': '6', 'שישה': '6', 'שבע': '7', 'שבעה': '7', 'שמונה': '8', 'תשע': '9', 'תשעה': '9',
    },
    'es': {
        'cero': '0', 'uno': '1', 'una': '1', 'dos': '2', 'tres': '3', 'cuatro': '4',
        'cinco': '5', 'seis': '6', 'siete': '7', 'ocho': '8', 'nueve': '9',
    },
    'fr': {
        'zéro': '0', 'zero': '0', 'un': '1', 'une': '1', 'deux': '2', 'trois': '3',
        'quatre': '4', 'cinq': '5', 'six': '6', 'sept': '7', 'huit': '8', 'neuf': '9',
    },
    'de': {
        'null': '0', 'eins': '1', 'zwei': '2', 'zwo': '2', 'drei': '3', 'vier': '4',
        'fünf': '5', 'funf': '5', 'sechs': '6', 'sieben': '7', 'acht': '8', 'neun': '9',
    },
    'it': {
        'zero': '0', 'uno': '1', 'una': '1', 'due': '2', 'tre': '3', 'quattro': '4',
        'cinque': '5', 'sei': '6', 'sette': '7', 'otto': '8', 'nove': '9',
    },
    'pt': {
        'zero': '0', 'um': '1', 'uma': '1', 'dois': '2', 'duas': '2', 'três': '3', 'tres': '3',
        'quatro': '4', 'cinco': '5', 'seis': '6', 'sete': '7', 'oito': '8', 'nove': '9',
    },
    'ru': {
        'ноль': '0', 'один': '1', 'одна': '1', 'два': '2', 'две': '2', 'три': '3',
        'четыре': '4', 'пять': '5', 'шесть': '6', 'семь': '7', 'восемь': '8', 'девять': '9',
    },
    'ar': {
        'صفر': '0', 'واحد': '1', 'اثنان': '2', 'ثلاثة': '3', 'أربعة': '4',
        'خمسة': '5', 'ستة': '6', 'سبعة': '7', 'ثمانية': '8', 'تسعة': '9',
    },
    'zh': {
        '零': '0', '一': '1', '二': '2', '两': '2', '三': '3', '四': '4',
        '五': '5', '六': '6', '七': '7', '八': '8', '九': '9',
    },
    'ja': {
        'ゼロ': '0', '零': '0', '一': '1', 'いち': '1', '二': '2', 'に': '2',
        '三': '3', 'さん': '3', '四': '4', 'よん': '4', 'し': '4',
        '五': '5', 'ご': '5', '六': '6', 'ろく': '6', '七': '7', 'なな': '7', 'しち': '7',
        '八': '8', 'はち': '8', '九': '9', 'きゅう': '9', 'く': '9',
    },
    'ko': {
        '영': '0', '공': '0', '일': '1', '하나': '1', '이': '2', '둘': '2',
        '삼': '3', '셋': '3', '사': '4', '넷': '4', '오': '5', '다섯': '5',
        '육': '6', '여섯': '6', '칠': '7', '일곱': '7', '팔': '8', '여덟': '8', '구': '9', '아홉': '9',
    },
    'pl': {
        'zero': '0', 'jeden': '1', 'jedna': '1', 'dwa': '2', 'dwie': '2', 'trzy': '3',
        'cztery': '4', 'pięć': '5', 'sześć': '6', 'siedem': '7', 'osiem': '8', 'dziewięć': '9',
    },
    'tr': {
        'sıfır': '0', 'bir': '1', 'iki': '2', 'üç': '3', 'uc': '3', 'dört': '4', 'dort': '4',
        'beş': '5', 'bes': '5', 'altı': '6', 'alti': '6', 'yedi': '7', 'sekiz': '8', 'dokuz': '9',
    },
    'nl': {
        'nul': '0', 'een': '1', 'één': '1', 'twee': '2', 'drie': '3', 'vier': '4',
        'vijf': '5', 'zes': '6', 'zeven': '7', 'acht': '8', 'negen': '9',
    },
    'hi': {
        'शून्य': '0', 'एक': '1', 'दो': '2', 'तीन': '3', 'चार': '4',
        'पांच': '5', 'छह': '6', 'सात': '7', 'आठ': '8', 'नौ': '9',
    },
}

# =============================================================================
# MULTI-LANGUAGE SENTIMENT DETECTION - For customer churn risk analysis
# =============================================================================
# Significantly expanded keywords for broader detection of dissatisfaction
NEGATIVE_SENTIMENT_KEYWORDS = {
    'en': [
        'angry', 'furious', 'frustrated', 'annoyed', 'upset', 'disappointed', 'terrible',
        'horrible', 'awful', 'worst', 'hate', 'disgusted', 'unacceptable', 'ridiculous',
        'cancel', 'leaving', 'switching', 'competitor', 'refund', 'sue', 'lawyer',
        'complaint', 'manager', 'supervisor', 'escalate', 'unbelievable', 'outrageous',
        'waste of time', 'useless', 'pathetic', 'incompetent', 'scam', 'fraud', 'rip off',
        'tired of', 'fed up', 'garbage', 'trash', 'broken', 'never again', 'stupid', 'idiot',
        'slow', 'buggy', 'glitchy', 'fails', 'failing', 'painful', 'nightmare', 'mess',
        'bad service', 'poor service', 'rude', 'insulting', 'ignoring me', 'no help',
    ],
    'he': [
        'כועס', 'מתוסכל', 'מאוכזב', 'נורא', 'איום', 'גרוע', 'שונא', 'מבאס',
        'לבטל', 'עוזב', 'מתחרה', 'החזר', 'תביעה', 'עורך דין', 'תלונה', 'מנהל',
        'לא מקובל', 'מגעיל', 'בושה', 'חוצפה', 'גניבה', 'רמאות', 'לא עובד', 'שבור',
        'חרא', 'זבל', 'בזבוז זמן', 'מיותר', 'לא יאמן', 'הזוי', 'נמאס לי', 'די',
        'לא רוצה', 'תנתקו אותי', 'שירות גרוע', 'יחס משפיל', 'מתעלמים', 'בושה וחרפה',
        'איטי', 'תקוע', 'לא זז', 'זוועה', 'קטסטרופה', 'נכשל', 'כישלון', 'לא עוזר',
    ],
    'es': [
        'enfadado', 'furioso', 'frustrado', 'molesto', 'decepcionado', 'terrible',
        'horrible', 'peor', 'odio', 'inaceptable', 'ridículo', 'cancelar', 'reembolso',
        'queja', 'gerente', 'supervisor', 'abogado', 'demanda', 'estafa', 'fraude',
        'basura', 'inútil', 'pérdida de tiempo', 'harto', 'cansado', 'vergüenza',
        'nunca más', 'roto', 'lento', 'pésimo', 'mal servicio', 'grosero', 'no funciona',
        'competencia', 'me voy', 'baja', 'rescindir', 'denuncia',
    ],
    'fr': [
        'en colère', 'furieux', 'frustré', 'déçu', 'terrible', 'horrible', 'pire',
        'déteste', 'inacceptable', 'ridicule', 'annuler', 'remboursement', 'plainte',
        'directeur', 'superviseur', 'avocat', 'procès', 'arnaque', 'fraude', 'nul',
        'inutile', 'perte de temps', 'marre', 'fatigué', 'honte', 'jamais plus',
        'cassé', 'lent', 'mauvais service', 'impoli', 'ne marche pas', 'compétiteur',
        'je pars', 'résilier', 'catastrophe', 'bordel',
    ],
    'de': [
        'wütend', 'frustriert', 'enttäuscht', 'schrecklich', 'furchtbar', 'schlimmste',
        'hasse', 'inakzeptabel', 'lächerlich', 'kündigen', 'erstattung', 'beschwerde',
        'manager', 'vorgesetzter', 'anwalt', 'klage', 'betrug', 'abzocke', 'nutzlos',
        'zeitverschwendung', 'satt', 'müde', 'schande', 'nie wieder', 'kaputt',
        'langsam', 'schlechter service', 'unhöflich', 'funktioniert nicht', 'konkurrenz',
        'ich gehe', 'stornieren', 'katastrophe', 'mist',
    ],
    'ru': [
        'злой', 'разочарован', 'расстроен', 'ужасный', 'отвратительный', 'худший',
        'ненавижу', 'неприемлемо', 'отмена', 'возврат', 'жалоба', 'менеджер', 'адвокат',
        'обман', 'мошенничество', 'бесполезно', 'трата времени', 'надоело', 'устал',
        'позор', 'никогда больше', 'сломано', 'медленно', 'плохой сервис', 'грубо',
        'не работает', 'конкурент', 'ухожу', 'расторгнуть', 'кошмар', 'ужас',
    ],
    'ar': [
        'غاضب', 'محبط', 'خائب', 'فظيع', 'مريع', 'أسوأ', 'أكره', 'غير مقبول',
        'إلغاء', 'استرداد', 'شكوى', 'مدير', 'محامي', 'احتيال', 'نصب', 'عديم الفائدة',
        'مضيعة للوقت', 'سئمت', 'تعبت', 'عار', 'لن أعود', 'مكسور', 'بطيء',
        'خدمة سيئة', 'وقح', 'لا يعمل', 'منافس', 'سأغادر', 'إنهاء', 'كارثة',
    ],
    'zh': [
        '生气', '愤怒', '失望', '沮丧', '糟糕', '可怕', '最差', '讨厌', '不可接受',
        '取消', '退款', '投诉', '经理', '律师', '骗局', '诈骗', '没用', '浪费时间',
        '受够了', '累了', '耻辱', '再也不', '坏了', '慢', '服务差', '粗鲁',
        '不工作', '竞争对手', '我要走', '终止', '垃圾', '坑人',
    ],
    'ja': [
        '怒り', '不満', '失望', 'ひどい', '最悪', '嫌い', '許せない', 'キャンセル',
        '返金', 'クレーム', 'マネージャー', '弁護士', '詐欺', '無駄', '時間の無駄',
        'うんざり', '疲れた', '恥', '二度と', '壊れた', '遅い', '悪いサービス',
        '失礼', '動かない', '競合', '辞める', '解約', 'ゴミ', '最低',
    ],
    'it': [
        'arrabbiato', 'furioso', 'frustrato', 'deluso', 'terribile', 'orribile',
        'odio', 'inaccettabile', 'ridicolo', 'cancellare', 'rimborso', 'reclamo',
        'avvocato', 'denuncia', 'responsabile', 'truffa', 'inutile', 'spreco di tempo',
        'stufo', 'stanco', 'vergogna', 'mai più', 'rotto', 'lento', 'pessimo servizio',
        'maleducato', 'non funziona', 'concorrenza', 'vado via', 'disdire',
    ],
    'pt': [
        'bravo', 'furioso', 'frustrado', 'decepcionado', 'terrível', 'horrível',
        'odeio', 'inaceitável', 'ridículo', 'cancelar', 'reembolso', 'reclamação',
        'gerente', 'supervisor', 'advogado', 'processo', 'golpe', 'fraude', 'inútil',
        'perda de tempo', 'farto', 'cansado', 'vergonha', 'nunca mais', 'quebrado',
        'lento', 'péssimo serviço', 'grosseiro', 'não funciona', 'concorrente',
        'vou sair', 'encerrar', 'lixo', 'porcaria',
    ],
    'tr': [
        'kızgın', 'öfkeli', 'hayal kırıklığı', 'berbat', 'korkunç', 'en kötü',
        'nefret', 'kabul edilemez', 'saçma', 'iptal', 'iade', 'şikayet',
        'müdür', 'yönetici', 'avukat', 'dava', 'dolandırıcılık', 'gereksiz',
        'zaman kaybı', 'bıktım', 'yoruldum', 'rezalet', 'bir daha asla', 'bozuk',
        'yavaş', 'kötü hizmet', 'kaba', 'çalışmıyor', 'rakip', 'gidiyorum',
        'kapatmak', 'lanet', 'rezil',
    ],
    'pl': [
        'zły', 'wściekły', 'sfrustrowany', 'rozczarowany', 'okropny', 'straszny',
        'najgorszy', 'nienawidzę', 'nieakceptowalne', 'śmieszne', 'anulować', 'zwrot',
        'skarga', 'kierownik', 'prawnik', 'oszustwo', 'bezużyteczne', 'strata czasu',
        'mam dość', 'zmęczony', 'wstyd', 'nigdy więcej', 'zepsute', 'wolne',
        'zła obsługa', 'niegrzeczny', 'nie działa', 'konkurencja', 'odchodzę',
        'zamknąć', 'beznadziejny', 'tragedia',
    ],
    'nl': [
        'boos', 'woedend', 'gefrustreerd', 'teleurgesteld', 'verschrikkelijk', 'vreselijk',
        'slechtste', 'haat', 'onacceptabel', 'belachelijk', 'annuleren', 'terugbetaling',
        'klacht', 'manager', 'advocaat', 'oplichting', 'nutteloos', 'tijdverspilling',
        'zat', 'moe', 'schande', 'nooit meer', 'kapot', 'traag', 'slechte service',
        'onbeleefd', 'werkt niet', 'concurrent', 'ik ga weg', 'opzeggen', 'waardeloos',
    ],
    'ko': [
        '화난', '분노', '좌절', '실망', '끔찍한', '최악', '싫어', '용납할 수 없는',
        '어이없는', '취소', '환불', '불만', '매니저', '변호사', '사기', '쓸모없는',
        '시간 낭비', '지겨운', '지친', '수치', '절대 다시는', '고장', '느린',
        '나쁜 서비스', '무례한', '작동 안함', '경쟁사', '떠날 거야', '해지', '엉망',
    ],
    'hi': [
        'gussa', 'naraz', 'niraash', 'bekaar', 'sabse bura', 'nafrat',
        'namumkin', 'radd', 'wapas', 'shikayat', 'manager', 'vakeel',
        'dhokha', 'scam', 'faltu', 'samay ki barbadi', 'thak gaya',
        'sharm', 'kabhi nahi', 'kharab', 'dheema', 'gandi service',
        'kaam nahi kar raha', 'chhod raha hu', 'band karo',
    ]
}

# Churn risk indicators - phrases that suggest customer might leave
# Expanded with more variations and languages
CHURN_INDICATORS = {
    'en': [
        'cancel subscription', 'close account', 'switch to', 'moving to', 'looking elsewhere', 
        'had enough', 'last chance', 'final warning', 'cancel my plan', 'stop service',
        'end my contract', 'unsubscribe', 'delete my account', 'remove me', 'too expensive',
        'found better', 'cheaper elsewhere', 'not worth it', 'waste of money', 'goodbye forever'
    ],
    'he': [
        'לבטל מנוי', 'לסגור חשבון', 'לעבור ל', 'מחפש מקום אחר', 'נמאס לי', 'הזדמנות אחרונה',
        'לבטל את התוכנית', 'להפסיק שירות', 'לסיים חוזה', 'להסיר אותי', 'יקר מדי',
        'מצאתי יותר טוב', 'יותר זול', 'לא שווה', 'בזבוז כסף', 'שלום ולא להתראות',
        'רוצה להתנתק', 'תנתקו אותי', 'עובר למתחרים'
    ],
    'es': [
        'cancelar suscripción', 'cerrar cuenta', 'cambiar a', 'buscar otro', 'harto',
        'última oportunidad', 'advertencia final', 'cancelar mi plan', 'detener servicio',
        'terminar contrato', 'darse de baja', 'borrar cuenta', 'eliminarme', 'muy caro',
        'encontré mejor', 'más barato', 'no vale la pena', 'pérdida de dinero', 'adiós para siempre'
    ],
    'fr': [
        'annuler abonnement', 'fermer compte', 'changer pour', 'chercher ailleurs',
        'j\'en ai assez', 'dernière chance', 'dernier avertissement', 'arrêter service',
        'finir contrat', 'se désabonner', 'supprimer compte', 'trop cher',
        'trouvé mieux', 'moins cher', 'ça ne vaut pas la peine', 'perte d\'argent', 'adieu'
    ],
    'de': [
        'abo kündigen', 'konto schließen', 'wechseln zu', 'woanders suchen',
        'habe genug', 'letzte chance', 'letzte warnung', 'plan kündigen', 'dienst stoppen',
        'vertrag beenden', 'abmelden', 'konto löschen', 'zu teuer',
        'besser gefunden', 'billiger woanders', 'nicht wert', 'geldverschwendung'
    ],
    'ru': [
        'отменить подписку', 'закрыть счет', 'перейти к', 'ищу другое',
        'хватит', 'последний шанс', 'последнее предупреждение', 'отменить план',
        'остановить сервис', 'расторгнуть договор', 'отписаться', 'удалить аккаунт',
        'слишком дорого', 'нашел лучше', 'дешевле', 'не стоит того', 'трата денег'
    ],
    'ar': [
        'إلغاء الاشتراك', 'إغلاق الحساب', 'التبديل إلى', 'البحث في مكان آخر',
        'طفح الكيل', 'فرصة أخيرة', 'تحذير أخير', 'إيقاف الخدمة', 'إنهاء العقد',
        'حذف حسابي', 'مكلف للغاية', 'وجدت أفضل', 'أرخص', 'لا يستحق', 'مضيعة للمال'
    ],
    'pt': [
        'cancelar assinatura', 'fechar conta', 'mudar para', 'procurar outro',
        'já chega', 'última chance', 'aviso final', 'parar serviço', 'encerrar contrato',
        'cancelar inscrição', 'excluir conta', 'muito caro', 'encontrei melhor',
        'mais barato', 'não vale a pena', 'perda de dinheiro', 'adeus'
    ],
    'it': [
        'cancellare abbonamento', 'chiudere account', 'passare a', 'cercare altrove',
        'ne ho abbastanza', 'ultima possibilità', 'ultimo avviso', 'fermare servizio',
        'terminare contratto', 'disiscriversi', 'eliminare account', 'troppo costoso',
        'trovato meglio', 'più economico', 'non ne vale la pena', 'spreco di denaro'
    ],
    'tr': [
        'aboneliği iptal et', 'hesabı kapat', 'başka yere geç', 'başka yer ara',
        'yeter artık', 'son şans', 'son uyarı', 'hizmeti durdur', 'sözleşmeyi bitir',
        'üyelikten çık', 'hesabımı sil', 'çok pahalı', 'daha iyisini buldum',
        'daha ucuz', 'değmez', 'para kaybı'
    ],
    'pl': [
        'anuluj subskrypcję', 'zamknij konto', 'zmień na', 'szukam gdzie indziej',
        'mam dość', 'ostatnia szansa', 'ostatnie ostrzeżenie', 'zatrzymaj usługę',
        'zakończ umowę', 'wypisz się', 'usuń konto', 'za drogo', 'znalazłem lepsze',
        'taniej', 'nie warto', 'strata pieniędzy'
    ],
    'nl': [
        'abonnement annuleren', 'account sluiten', 'overstappen naar', 'ergens anders zoeken',
        'genoeg gehad', 'laatste kans', 'laatste waarschuwing', 'service stoppen',
        'contract beëindigen', 'uitschrijven', 'account verwijderen', 'te duur',
        'beter gevonden', 'goedkoper', 'niet waard', 'geldverspilling'
    ],
}

# =============================================================================
# TTS CACHE - Cache common phrases to reduce TTS calls
# =============================================================================
_tts_cache = SimpleCache(default_ttl=3600)  # 1 hour TTL for TTS cache

# =============================================================================
# CONNECTION WATCHDOG - Monitor service health
# =============================================================================
class ConnectionWatchdog:
    """Monitors connection health and reports issues"""
    
    def __init__(self):
        self.last_successful_asr = time.time()
        self.last_successful_tts = time.time()
        self.last_successful_llm = time.time()
        self.alert_threshold = 60  # seconds
    
    def record_asr_success(self):
        self.last_successful_asr = time.time()
    
    def record_tts_success(self):
        self.last_successful_tts = time.time()
    
    def record_llm_success(self):
        self.last_successful_llm = time.time()
    
    def check_health(self) -> list:
        now = time.time()
        issues = []
        if now - self.last_successful_asr > self.alert_threshold:
            issues.append(f"ASR: No success in {int(now - self.last_successful_asr)}s")
        if now - self.last_successful_tts > self.alert_threshold:
            issues.append(f"TTS: No success in {int(now - self.last_successful_tts)}s")
        if now - self.last_successful_llm > self.alert_threshold:
            issues.append(f"LLM: No success in {int(now - self.last_successful_llm)}s")
        return issues

_watchdog = ConnectionWatchdog()

# =============================================================================
# NATURAL RESPONSE TEMPLATES - For human-like TTS output
# XTTS v2 Supported: en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh, ja, hu, ko, hi
# =============================================================================
NATURAL_RESPONSES = {
    "en": {
        "greeting_new": [
            "Hey there! Welcome to our service. I'll need to verify your account real quick - could you tell me your four digit PIN?",
            "Hi! Thanks for calling. Before we get started, I just need your four digit PIN to pull up your account.",
            "Hello! Great to hear from you. For security, could you share your four digit PIN with me?",
        ],
        "greeting_returning": [
            "Hey {name}! Good to have you back. What can I do for you today?",
            "Hi {name}! How's it going? What brings you in today?",
            "{name}! Nice to hear from you again. How can I help?",
        ],
        "pin_invalid": [
            "Hmm, that PIN didn't match what I have on file. You've got {remaining} more tries - want to give it another shot?",
            "That one didn't work, I'm afraid. {remaining} attempts left. Go ahead and try again.",
        ],
        "pin_unclear": [
            "Sorry, I didn't quite catch that. Could you say your four digit PIN again for me?",
            "I missed that - could you repeat your PIN? Just the four digits.",
        ],
        "pin_locked": [
            "I'm really sorry, but we've hit the limit on PIN attempts. You'll need to contact our support team directly.",
        ],
        "upgrade_ask_plan": [
            "Sure thing! So you're thinking about upgrading. We've got a few options. Are you looking at Standard or Premium?",
            "Absolutely! What are you thinking - Standard upgrade or Premium?",
        ],
        "upgrade_confirm": [
            "Perfect, so you want to upgrade to {plan}. Just to confirm - should I put in a request to switch you to {plan}?",
            "Got it - {plan} it is! Can you confirm that's the plan you want?",
        ],
        "upgrade_submitted": [
            "Done! I've submitted your upgrade request for {plan}. One of our team members will call you shortly to finalize everything.",
            "Perfect, your {plan} upgrade request is in. Expect a call from us soon to complete the process.",
        ],
        "upgrade_cancelled": [
            "No problem at all! Let me know if you change your mind.",
            "Got it - no changes for now. Anything else I can help with?",
        ],
        "info_not_found": [
            "Hmm, I couldn't find any {type} information on your account. Want me to check something else?",
        ],
        "query_error": [
            "Oh, I ran into a little snag looking that up. Mind if we try again?",
        ],
        "goodbye": [
            "Take care! Don't hesitate to call if you need anything.",
            "Thanks for calling! Have a great day!",
        ],
        "ticket_ask_details": [
            "Sure, I can open a ticket for you. Why do you want to open a ticket? Please describe the issue.",
            "I can help with that. What is the reason for the ticket?",
            "No problem. Please tell me why you need a ticket so I can note it down.",
        ],
        "ticket_created": [
            "I've opened that ticket for you. Your ticket number is {id}. Our team will look into it shortly.",
            "Done. Ticket #{id} has been created. We'll get back to you as soon as possible.",
        ],
    },
    "es": {
        "greeting_new": [
            "¡Hola! Bienvenido a nuestro servicio. Necesito verificar tu cuenta - ¿podrías decirme tu PIN de cuatro dígitos?",
        ],
        "greeting_returning": [
            "¡Hola {name}! Qué bueno tenerte de vuelta. ¿En qué puedo ayudarte hoy?",
        ],
        "pin_invalid": [
            "Ese PIN no coincide. Te quedan {remaining} intentos más. ¿Quieres intentar de nuevo?",
        ],
        "pin_unclear": [
            "Perdona, no escuché bien. ¿Podrías repetir tu PIN de cuatro dígitos?",
        ],
        "pin_locked": [
            "Lo siento, has alcanzado el límite de intentos. Necesitarás contactar a nuestro equipo de soporte.",
        ],
        "upgrade_ask_plan": [
            "¡Claro! ¿Estás pensando en Standard o Premium?",
        ],
        "upgrade_confirm": [
            "Perfecto, quieres actualizar a {plan}. ¿Confirmas que quieres cambiar a {plan}?",
        ],
        "upgrade_submitted": [
            "¡Listo! He enviado tu solicitud de actualización a {plan}. Un representante te llamará pronto.",
        ],
        "upgrade_cancelled": [
            "¡Sin problema! Avísame si cambias de opinión.",
        ],
        "info_not_found": [
            "No encontré información de {type} en tu cuenta. ¿Quieres que busque otra cosa?",
        ],
        "query_error": [
            "Tuve un pequeño problema buscando eso. ¿Intentamos de nuevo?",
        ],
        "goodbye": [
            "¡Cuídate! No dudes en llamar si necesitas algo.",
        ],
        "ticket_ask_details": [
            "Claro, puedo abrir un ticket. ¿Por qué quieres abrir un ticket? Por favor describe el problema.",
            "Puedo ayudarte con eso. ¿Cuál es la razón del ticket?",
        ],
        "ticket_created": [
            "He abierto el ticket. El número es {id}. Nuestro equipo lo revisará pronto.",
            "Listo. Ticket #{id} creado correctamente.",
        ],
    },
    "fr": {
        "greeting_new": [
            "Bonjour! Bienvenue. J'ai besoin de vérifier votre compte - pouvez-vous me donner votre code PIN à quatre chiffres?",
        ],
        "greeting_returning": [
            "Bonjour {name}! Content de vous revoir. Comment puis-je vous aider aujourd'hui?",
        ],
        "pin_invalid": [
            "Ce code ne correspond pas. Il vous reste {remaining} essais. Voulez-vous réessayer?",
        ],
        "pin_unclear": [
            "Désolé, je n'ai pas bien entendu. Pouvez-vous répéter votre code PIN?",
        ],
        "pin_locked": [
            "Désolé, vous avez atteint la limite d'essais. Veuillez contacter notre équipe de support.",
        ],
        "upgrade_ask_plan": [
            "Bien sûr! Vous pensez à Standard ou Premium?",
        ],
        "upgrade_confirm": [
            "Parfait, vous voulez passer à {plan}. Je confirme la demande de {plan}?",
        ],
        "upgrade_submitted": [
            "C'est fait! J'ai soumis votre demande de mise à niveau vers {plan}. Un représentant vous contactera bientôt.",
        ],
        "upgrade_cancelled": [
            "Pas de problème! Dites-moi si vous changez d'avis.",
        ],
        "info_not_found": [
            "Je n'ai pas trouvé d'informations sur {type}. Voulez-vous que je cherche autre chose?",
        ],
        "query_error": [
            "J'ai eu un petit problème. On réessaie?",
        ],
        "goodbye": [
            "Prenez soin de vous! N'hésitez pas à rappeler.",
        ],
        "ticket_ask_details": [
            "Bien sûr, je peux ouvrir un ticket. Pourquoi voulez-vous ouvrir un ticket ? Veuillez décrire le problème.",
            "Je peux vous aider. Quelle est la raison du ticket ?",
        ],
        "ticket_created": [
            "J'ai ouvert le ticket pour vous. Votre numéro est {id}.",
            "C'est fait. Le ticket #{id} a été créé.",
        ],
    },
    "de": {
        "greeting_new": [
            "Hallo! Willkommen. Ich muss Ihr Konto verifizieren - können Sie mir Ihre vierstellige PIN sagen?",
        ],
        "greeting_returning": [
            "Hallo {name}! Schön, Sie wiederzuhören. Wie kann ich Ihnen heute helfen?",
        ],
        "pin_invalid": [
            "Diese PIN stimmt nicht. Sie haben noch {remaining} Versuche. Möchten Sie es nochmal versuchen?",
        ],
        "pin_unclear": [
            "Entschuldigung, ich habe das nicht verstanden. Können Sie Ihre PIN wiederholen?",
        ],
        "pin_locked": [
            "Es tut mir leid, Sie haben die maximale Anzahl an Versuchen erreicht. Bitte kontaktieren Sie unseren Support.",
        ],
        "upgrade_ask_plan": [
            "Natürlich! Denken Sie an Standard oder Premium?",
        ],
        "upgrade_confirm": [
            "Perfekt, Sie möchten auf {plan} upgraden. Soll ich die Anfrage für {plan} einreichen?",
        ],
        "upgrade_submitted": [
            "Erledigt! Ich habe Ihre Upgrade-Anfrage für {plan} eingereicht. Ein Mitarbeiter wird Sie bald kontaktieren.",
        ],
        "upgrade_cancelled": [
            "Kein Problem! Sagen Sie Bescheid, wenn Sie es sich anders überlegen.",
        ],
        "info_not_found": [
            "Ich konnte keine {type}-Informationen finden. Soll ich etwas anderes suchen?",
        ],
        "query_error": [
            "Da ist ein kleines Problem aufgetreten. Versuchen wir es nochmal?",
        ],
        "goodbye": [
            "Auf Wiederhören! Rufen Sie gerne wieder an.",
        ],
        "ticket_ask_details": [
            "Sicher, ich kann ein Ticket öffnen. Warum möchten Sie ein Ticket öffnen? Bitte beschreiben Sie das Problem.",
            "Kein Problem. Was ist der Grund für das Ticket?",
        ],
        "ticket_created": [
            "Ich habe das Ticket für Sie geöffnet. Ihre Ticketnummer ist {id}.",
            "Erledigt. Ticket #{id} wurde erstellt.",
        ],
    },
    "he": {
        "greeting_new": [
            "היי! ברוכים הבאים. אני צריך לאמת את החשבון שלך - תוכל לומר לי את קוד ה-PIN בן ארבע הספרות?",
        ],
        "greeting_returning": [
            "היי {name}! טוב שחזרת. איך אני יכול לעזור לך היום?",
        ],
        "pin_invalid": [
            "הקוד לא תואם. נשארו לך {remaining} ניסיונות. רוצה לנסות שוב?",
        ],
        "pin_unclear": [
            "סליחה, לא שמעתי טוב. תוכל לחזור על הקוד?",
        ],
        "pin_locked": [
            "מצטער, הגעת למגבלת הניסיונות. צריך לפנות לצוות התמיכה.",
        ],
        "upgrade_ask_plan": [
            "בטח! אתם חושבים על סטנדרט או פרימיום?",
        ],
        "upgrade_confirm": [
            "מעולה, אתם רוצים לשדרג ל-{plan}. לאשר את הבקשה?",
        ],
        "upgrade_submitted": [
            "בוצע! הגשתי את בקשת השדרוג ל-{plan}. נציג יחזור אליך בקרוב.",
        ],
        "upgrade_cancelled": [
            "אין בעיה! תודיע לי אם תשנה את דעתך.",
        ],
        "info_not_found": [
            "לא מצאתי מידע על {type}. רוצה שאחפש משהו אחר?",
        ],
        "query_error": [
            "הייתה לי בעיה קטנה בחיפוש. שננסה שוב?",
        ],
        "goodbye": [
            "להתראות! אל תהסס להתקשר אם תצטרך משהו.",
        ],
        "ticket_ask_details": [
            "אין בעיה, אני אפתח לך קריאה. למה אתה רוצה לפתוח קריאה? תאר לי את הבעיה.",
            "בטח, אני אפתח כרטיס תמיכה. מה הסיבה לפנייה?",
            "אני יכול לעזור עם זה. ספר לי למה אתה צריך קריאה כדי שאוכל לרשום.",
        ],
        "ticket_created": [
            "פתחתי לך את הקריאה. מספר הקריאה הוא {id}. הצוות שלנו יטפל בזה בהקדם.",
            "בוצע. קריאה מספר {id} נפתחה בהצלחה. נחזור אליך בקרוב.",
            "הכל מסודר. פתחתי קריאה מספר {id} עם הפרטים שמסרת.",
        ],
    },
}

def get_natural_response(key, lang="en", **kwargs):
    """Get a random natural response template and format it"""
    import random
    # Fallback to English if language not found
    lang_responses = NATURAL_RESPONSES.get(lang, NATURAL_RESPONSES.get("en", {}))
    templates = lang_responses.get(key, NATURAL_RESPONSES.get("en", {}).get(key, ["I'm here to help."]))
    template = random.choice(templates)
    return template.format(**kwargs) if kwargs else template


# =============================================================================
# Pre-defined SQL queries for common operations
# =============================================================================
PREDEFINED_QUERIES = {
    'subscription': """
        SELECT c.name as customer_name, p.name as plan_name, p.price, 
               s.status, s.start_date, s.end_date, s.auto_renew
        FROM subscriptions s
        JOIN customers c ON s.customer_id = c.id
        JOIN plans p ON s.plan_id = p.id
        WHERE s.customer_id = %s
        ORDER BY s.start_date DESC
        LIMIT 5
    """,
    'balance': """
        SELECT 
            COALESCE(SUM(CASE WHEN i.status = 'pending' THEN i.amount ELSE 0 END), 0) as pending_amount,
            COALESCE(SUM(CASE WHEN i.status = 'paid' THEN i.amount ELSE 0 END), 0) as paid_amount,
            COALESCE(SUM(CASE WHEN i.status = 'overdue' THEN i.amount ELSE 0 END), 0) as overdue_amount
        FROM invoices i
        WHERE i.customer_id = %s
    """,
    'invoices': """
        SELECT i.id, i.amount, i.status, i.due_date, i.paid_at,
               p.name as plan_name
        FROM invoices i
        JOIN subscriptions s ON i.subscription_id = s.id
        JOIN plans p ON s.plan_id = p.id
        WHERE i.customer_id = %s
        ORDER BY i.due_date DESC
        LIMIT 10
    """,
    'plan': """
        SELECT p.name, p.price, p.billing_cycle, p.features, 
               p.data_limit_gb, p.support_level
        FROM subscriptions s
        JOIN plans p ON s.plan_id = p.id
        WHERE s.customer_id = %s AND s.status = 'active'
        LIMIT 1
    """,
    'tickets': """
        SELECT id, subject, status, priority, created_at
        FROM support_tickets
        WHERE customer_id = %s
        ORDER BY created_at DESC
        LIMIT 10
    """,
    'customer_info': """
        SELECT id, name, email, phone, created_at
        FROM customers
        WHERE id = %s
    """,
    'upgrade_options': """
        SELECT p.id, p.name, p.price, p.data_limit_gb, p.support_level, p.features
        FROM plans p
        WHERE p.price > (
            SELECT pl.price FROM subscriptions s 
            JOIN plans pl ON s.plan_id = pl.id 
            WHERE s.customer_id = %s AND s.status = 'active'
            LIMIT 1
        )
        ORDER BY p.price ASC
        LIMIT 3
    """,
    'current_plan': """
        SELECT p.id, p.name, p.price
        FROM subscriptions s
        JOIN plans p ON s.plan_id = p.id
        WHERE s.customer_id = %s AND s.status = 'active'
        LIMIT 1
    """,
    'all_plans': """
        SELECT id, name, price, data_limit_gb, support_level
        FROM plans
        ORDER BY price ASC
    """
}

# Keywords that trigger each query type (English and Hebrew support)
# IMPORTANT: Specific actions must come BEFORE general queries to avoid shadowing
QUERY_TRIGGERS = {
    'create_ticket': [
        # English
        'open ticket', 'new ticket', 'create ticket', 'report issue', 'have a problem', 'something is wrong',
        'start ticket', 'file ticket', 'submit ticket',
        'open a ticket', 'open my ticket', 'create a ticket', 'create new ticket', 'start a ticket',
        'i want to open', 'need to open',
        # Hebrew
        'לפתוח קריאה', 'לפתוח כרטיס', 'כרטיס חדש', 'קריאה חדשה', 'יש לי בעיה', 'משהו לא עובד', 'לדווח על תקלה',
        'תפתח קריאה', 'תפתח כרטיס', 'פתח קריאה', 'פתח כרטיס', 'לפתוח טיקט', 'תפתח טיקט', 'טיקט חדש',
        'אני רוצה לפתוח', 'יש לי תקלה', 'לדווח על בעיה',
        # Spanish
        'abrir ticket', 'nuevo ticket', 'crear ticket', 'reportar problema', 'tengo un problema',
        'abrir incidencia', 'nueva incidencia', 'crear incidencia',
        # French
        'ouvrir ticket', 'nouveau ticket', 'créer ticket', 'signaler problème', 'j\'ai un problème',
        'ouvrir incident', 'créer incident',
        # German
        'ticket öffnen', 'neues ticket', 'ticket erstellen', 'problem melden', 'ich habe ein problem',
        'ticket aufmachen',
        # Russian
        'открыть тикет', 'новый тикет', 'создать тикет', 'сообщить о проблеме', 'у меня проблема',
        'открыть заявку', 'новая заявка', 'создать заявку',
        # Arabic
        'فتح تذكرة', 'تذكرة جديدة', 'إنشاء تذكرة', 'الإبلاغ عن مشكلة', 'لدي مشكلة',
        # Portuguese
        'abrir ticket', 'novo ticket', 'criar ticket', 'reportar problema', 'tenho um problema',
        'abrir chamado', 'novo chamado',
        # Italian
        'aprire ticket', 'nuovo ticket', 'creare ticket', 'segnalare problema', 'ho un problema',
        'aprire segnalazione',
        # Chinese
        '打开工单', '新建工单', '创建工单', '报告问题', '我有问题',
        # Japanese
        'チケットを開く', '新しいチケット', 'チケットを作成', '問題を報告', '問題があります'
    ],
    'close_ticket': [
        # English
        'close ticket', 'resolve ticket', 'mark as resolved', 'ticket is done', 'fix issue',
        'close the ticket', 'ticket resolved', 'issue fixed',
        'close my ticket', 'close a ticket', 'close this ticket', 'close that ticket',
        'resolve my ticket', 'resolve this ticket',
        'i want to close', 'can you close',
        # Hebrew
        'לסגור קריאה', 'לסגור כרטיס', 'לסגור את הכרטיס', 'תסגור כרטיס', 'בעיה נפתרה', 'תקלה נפתרה', 'לסגור',
        'לסגור support ticket', 'לסגור support tickets', 'לסגור טיקט', 'לסגור את הטיקט', 'לסגור ticket',
        'לסגור את הקריאה', 'לסגור את הטיקט', 'תסגור את הקריאה',
        # Spanish
        'cerrar ticket', 'resolver ticket', 'ticket resuelto', 'problema solucionado',
        'cerrar mi ticket', 'cerrar incidencia',
        # French
        'fermer ticket', 'résoudre ticket', 'ticket résolu', 'problème réglé',
        'fermer mon ticket', 'clôturer ticket',
        # German
        'ticket schließen', 'ticket lösen', 'ticket erledigt', 'problem gelöst',
        'mein ticket schließen',
        # Russian
        'закрыть тикет', 'решить тикет', 'тикет решен', 'проблема решена',
        'закрыть заявку', 'закрыть мою заявку',
        # Arabic
        'إغلاق التذكرة', 'حل التذكرة', 'تم حل المشكلة', 'إغلاق تذكرتي',
        # Portuguese
        'fechar ticket', 'resolver ticket', 'ticket resolvido', 'problema resolvido',
        'fechar chamado', 'encerrar ticket',
        # Italian
        'chiudere ticket', 'risolvere ticket', 'ticket risolto', 'problema risolto',
        'chiudere segnalazione',
        # Chinese
        '关闭工单', '解决工单', '工单已解决', '问题已解决',
        # Japanese
        'チケットを閉じる', 'チケットを解決', 'チケット完了', '問題解決'
    ],
    'upgrade': [
        # English
        'upgrade', 'upgrading', 'higher plan', 'better plan', 'more features', 'switch plan', 'change plan',
        # Hebrew
        'לשדרג', 'שדרוג', 'תוכנית טובה יותר', 'להחליף תוכנית', 'חבילה גדולה יותר',
        # Spanish
        'actualizar plan', 'mejorar plan', 'cambiar plan', 'plan superior', 'más funciones',
        # French
        'mettre à niveau', 'changer de forfait', 'plan supérieur', 'meilleur forfait',
        # German
        'upgraden', 'plan ändern', 'besserer plan', 'höherer plan', 'tarif wechseln',
        # Russian
        'обновить план', 'улучшить план', 'сменить план', 'повысить план', 'лучший тариф',
        # Arabic
        'ترقية الخطة', 'تحسين الخطة', 'تغيير الخطة', 'خطة أفضل',
        # Portuguese
        'atualizar plano', 'melhorar plano', 'mudar plano', 'plano superior',
        # Italian
        'aggiornare piano', 'migliorare piano', 'cambiare piano', 'piano superiore'
    ],
    'subscription': [
        # English
        'subscription', 'subscribed', 'subscribe', 'membership', 'member', 'my plan',
        # Hebrew
        'מנוי', 'התוכנית שלי', 'חבילה', 'רשום',
        # Spanish
        'suscripción', 'suscribirse', 'mi plan', 'membresía',
        # French
        'abonnement', 'souscrire', 'mon forfait', 'adhésion',
        # German
        'abonnement', 'abo', 'mein plan', 'mitgliedschaft',
        # Russian
        'подписка', 'абонемент', 'мой план', 'членство',
        # Arabic
        'اشتراك', 'خطتي', 'عضويتي',
        # Portuguese
        'assinatura', 'inscrição', 'meu plano',
        # Italian
        'abbonamento', 'iscrizione', 'il mio piano'
    ],
    'balance': [
        # English
        'balance', 'owe', 'owing', 'due', 'payment', 'pay', 'amount',
        # Hebrew
        'יתרה', 'חוב', 'תשלום', 'לשלם', 'כמה אני חייב',
        # Spanish
        'saldo', 'debe', 'deuda', 'pagar', 'pago', 'monto',
        # French
        'solde', 'dois', 'paiement', 'payer', 'montant',
        # German
        'kontostand', 'saldo', 'schulden', 'zahlen', 'zahlung', 'betrag',
        # Russian
        'баланс', 'долг', 'оплата', 'платить', 'сумма', 'сколько я должен',
        # Arabic
        'رصيد', 'دين', 'دفع', 'مبلغ', 'كم علي',
        # Portuguese
        'saldo', 'devo', 'pagamento', 'pagar', 'quantia',
        # Italian
        'saldo', 'debiti', 'pagamento', 'pagare', 'importo'
    ],
    'invoices': [
        # English
        'invoice', 'invoices', 'bill', 'bills', 'billing', 'receipt',
        # Hebrew
        'חשבונית', 'חשבוניות', 'קבלה', 'חיוב',
        # Spanish
        'factura', 'facturas', 'recibo', 'cobro',
        # French
        'facture', 'factures', 'reçu', 'facturation',
        # German
        'rechnung', 'rechnungen', 'beleg', 'abrechnung',
        # Russian
        'счет', 'счета', 'квитанция', 'оплата',
        # Arabic
        'فاتورة', 'فواتير', 'إيصال',
        # Portuguese
        'fatura', 'faturas', 'recibo', 'cobrança',
        # Italian
        'fattura', 'fatture', 'ricevuta'
    ],
    'plan': [
        # English
        'plan details', 'my package', 'current plan', 'what plan', 'features', 'data limit',
        # Hebrew
        'פרטי תוכנית', 'מה החבילה', 'כמה דאטה', 'תכונות',
        # Spanish
        'detalles del plan', 'mi paquete', 'plan actual', 'qué plan', 'características',
        # French
        'détails du forfait', 'mon paquet', 'plan actuel', 'quel forfait', 'fonctionnalités',
        # German
        'plandetails', 'mein paket', 'aktueller plan', 'welcher plan', 'funktionen',
        # Russian
        'детали плана', 'мой пакет', 'текущий план', 'какой план', 'функции',
        # Arabic
        'تفاصيل الخطة', 'باقتي', 'الخطة الحالية', 'مميزات',
        # Portuguese
        'detalhes do plano', 'meu pacote', 'plano atual', 'qual plano', 'recursos',
        # Italian
        'dettagli piano', 'mio pacchetto', 'piano attuale', 'funzionalità'
    ],
    'tickets': [
        # English
        'ticket', 'tickets', 'support request', 'issue', 'problem', 'help request',
        # Hebrew
        'כרטיס', 'קריאה', 'תקלה', 'בעיה', 'תמיכה',
        # Spanish
        'ticket', 'tickets', 'solicitud de soporte', 'problema', 'incidencia',
        # French
        'ticket', 'tickets', 'demande d\'assistance', 'problème', 'incident',
        # German
        'ticket', 'tickets', 'support-anfrage', 'problem', 'anliegen',
        # Russian
        'тикет', 'тикеты', 'запрос в поддержку', 'проблема', 'вопрос',
        # Arabic
        'تذكرة', 'تذاكر', 'طلب دعم', 'مشكلة',
        # Portuguese
        'ticket', 'tickets', 'solicitação de suporte', 'problema',
        # Italian
        'ticket', 'tickets', 'richiesta di supporto', 'problema'
    ],
    'customer_info': [
        # English
        'account', 'profile', 'my info', 'my information', 'my details',
        # Hebrew
        'חשבון', 'פרופיל', 'פרטים שלי', 'מידע שלי',
        # Spanish
        'cuenta', 'perfil', 'mi información', 'mis datos',
        # French
        'compte', 'profil', 'mes infos', 'mes informations',
        # German
        'konto', 'profil', 'meine infos', 'meine daten',
        # Russian
        'аккаунт', 'профиль', 'моя информация', 'мои данные',
        # Arabic
        'حساب', 'ملف شخصي', 'معلوماتي', 'بياناتي',
        # Portuguese
        'conta', 'perfil', 'minhas informações', 'meus dados',
        # Italian
        'account', 'profilo', 'mie informazioni', 'miei dati'
    ]
}

# Conversation memory store (session_id -> ConversationState)
conversation_sessions = {}


# =============================================================================
# CONVERSATION STATE - Enhanced with upgrade flow
# =============================================================================
class ConversationState:
    """Maintains conversation state for a session"""
    
    # Conversation states
    STATE_NEW = "new"
    STATE_AWAITING_PIN = "awaiting_pin"
    STATE_IDENTIFIED = "identified"
    STATE_UPGRADE_SELECT_PLAN = "upgrade_select_plan"
    STATE_UPGRADE_CONFIRM = "upgrade_confirm"
    STATE_CREATE_TICKET = "create_ticket"
    
    def __init__(self):
        self.customer_id = None
        self.customer_name = None
        self.customer_email = None
        self.customer_pin = None
        self.is_identified = False
        self.awaiting_pin = False
        self.pin_attempts = 0
        self.history = []
        self.last_activity = datetime.now()
        
        # Enhanced state tracking
        self.state = self.STATE_NEW
        self.pending_upgrade_plan = None  # Plan name being upgraded to
        self.pending_upgrade_plan_id = None  # Plan ID in database
    
    def add_message(self, role, message):
        self.history.append({
            "role": role, 
            "content": message, 
            "timestamp": datetime.now().isoformat()
        })
        self.last_activity = datetime.now()
        # Keep last 20 messages for context
        if len(self.history) > 20:
            self.history = self.history[-20:]
    
    def get_context(self, num_messages=6):
        """Get recent conversation context for LLM"""
        context = ""
        for msg in self.history[-num_messages:]:
            role = "Customer" if msg["role"] == "user" else "Agent"
            context += f"{role}: {msg['content']}\n"
        return context
    
    def identify_customer(self, customer_id, name, email, pin=None):
        self.customer_id = customer_id
        self.customer_name = name
        self.customer_email = email
        self.customer_pin = pin
        self.is_identified = True
        self.awaiting_pin = False
        self.state = self.STATE_IDENTIFIED
        logging.info(f"[Session] Customer identified: {name} (ID: {customer_id})")
    
    def start_upgrade_flow(self):
        """Start the upgrade selection process"""
        self.state = self.STATE_UPGRADE_SELECT_PLAN
        self.pending_upgrade_plan = None
        self.pending_upgrade_plan_id = None
    
    def set_pending_upgrade(self, plan_name, plan_id=None):
        """Set the plan user wants to upgrade to"""
        self.pending_upgrade_plan = plan_name
        self.pending_upgrade_plan_id = plan_id
        self.state = self.STATE_UPGRADE_CONFIRM
    
    def complete_upgrade(self):
        """Complete the upgrade flow"""
        plan = self.pending_upgrade_plan
        self.pending_upgrade_plan = None
        self.pending_upgrade_plan_id = None
        self.state = self.STATE_IDENTIFIED
        return plan
    
    def cancel_upgrade(self):
        """Cancel pending upgrade"""
        self.pending_upgrade_plan = None
        self.pending_upgrade_plan_id = None
        self.state = self.STATE_IDENTIFIED
    
    def reset(self):
        """Reset the session state"""
        self.customer_id = None
        self.customer_name = None
        self.customer_email = None
        self.customer_pin = None
        self.is_identified = False
        self.awaiting_pin = False
        self.pin_attempts = 0
        self.history = []
        self.state = self.STATE_NEW
        self.pending_upgrade_plan = None
        self.pending_upgrade_plan_id = None
        logging.info("[Session] Session reset")

    def to_dict(self):
        """Serialize state to dictionary"""
        return {
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "customer_email": self.customer_email,
            "customer_pin": self.customer_pin,
            "is_identified": self.is_identified,
            "awaiting_pin": self.awaiting_pin,
            "pin_attempts": self.pin_attempts,
            "history": self.history,
            "state": self.state,
            "pending_upgrade_plan": self.pending_upgrade_plan,
            "pending_upgrade_plan_id": self.pending_upgrade_plan_id,
            "last_activity": self.last_activity.isoformat()
        }

    @classmethod
    def from_dict(cls, data):
        """Deserialize state from dictionary"""
        state = cls()
        state.customer_id = data.get("customer_id")
        state.customer_name = data.get("customer_name")
        state.customer_email = data.get("customer_email")
        state.customer_pin = data.get("customer_pin")
        state.is_identified = data.get("is_identified", False)
        state.awaiting_pin = data.get("awaiting_pin", False)
        state.pin_attempts = data.get("pin_attempts", 0)
        state.history = data.get("history", [])
        state.state = data.get("state", cls.STATE_NEW)
        state.pending_upgrade_plan = data.get("pending_upgrade_plan")
        state.pending_upgrade_plan_id = data.get("pending_upgrade_plan_id")
        
        last_activity = data.get("last_activity")
        if last_activity:
            try:
                state.last_activity = datetime.fromisoformat(last_activity)
            except:
                state.last_activity = datetime.now()
        
        return state


# =============================================================================
# DATABASE MANAGER - Enhanced with asyncpg
# =============================================================================
# Global flag to ensure table is only cleared once per server startup
_upgrade_table_initialized = False

class DatabaseManager:
    """Manages PostgreSQL database connections and queries using asyncpg"""
    
    def __init__(self, host, port, dbname, user, password, pool=None):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self.pool = pool
        self._owns_pool = pool is None
    
    async def connect(self):
        global _upgrade_table_initialized
        if not ASYNCPG_AVAILABLE:
            logging.error("asyncpg not available")
            return False
            
        # If pool was provided externally, we are already connected
        if self.pool:
            # Still check initialization if needed using the provided pool
            if not _upgrade_table_initialized:
                try:
                    await self._initialize_upgrade_requests_table()
                    _upgrade_table_initialized = True
                except Exception as e:
                    logging.warning(f"Failed to init tables with shared pool: {e}")
            return True
            
        try:
            self.pool = await asyncpg.create_pool(
                host=self.host,
                port=self.port,
                database=self.dbname,
                user=self.user,
                password=self.password,
                min_size=1,
                max_size=10
            )
            self._owns_pool = True
            logging.info(f"Connected to database (new pool): {self.dbname}@{self.host}")
            
            # Initialize tables only ONCE per server startup (global flag)
            if not _upgrade_table_initialized:
                await self._initialize_upgrade_requests_table()
                _upgrade_table_initialized = True
            
            return True
        except Exception as e:
            logging.error(f"Database connection failed: {e}")
            return False
    
    async def _initialize_upgrade_requests_table(self):
        """Create upgrade_requests table if not exists and clear ALL previous requests on startup"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as connection:
                # Create table if not exists
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS upgrade_requests (
                        id SERIAL PRIMARY KEY,
                        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                        customer_name VARCHAR(255),
                        customer_email VARCHAR(255),
                        current_plan_id INTEGER,
                        current_plan_name VARCHAR(255),
                        target_plan_id INTEGER,
                        target_plan_name VARCHAR(255),
                        status VARCHAR(50) DEFAULT 'pending',
                        priority VARCHAR(20) DEFAULT 'normal',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        processed_at TIMESTAMP,
                        processed_by VARCHAR(255),
                        notes TEXT,
                        session_id VARCHAR(255)
                    )
                """)
                
                # Create index for faster queries
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_upgrade_requests_status 
                    ON upgrade_requests(status)
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_upgrade_requests_customer 
                    ON upgrade_requests(customer_id)
                """)
                
                # CLEAR ALL previous requests on server startup (start fresh)
                await connection.execute("DELETE FROM upgrade_requests")
                
                # Reset the sequence to start from 1
                await connection.execute("ALTER SEQUENCE upgrade_requests_id_seq RESTART WITH 1")
                
                logging.info("[DB] Upgrade requests table initialized (cleared all previous requests)")
                
                # Initialize other tables
                await self._initialize_support_tickets_table()
                await self._initialize_sentiment_alerts_table()
                await self._initialize_session_storage_table()
                
        except Exception as e:
            logging.warning(f"[DB] Could not initialize upgrade_requests table: {e}")

    async def _initialize_support_tickets_table(self):
        """Create support_tickets table if not exists"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as connection:
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS support_tickets (
                        id SERIAL PRIMARY KEY,
                        customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE,
                        subject VARCHAR(255),
                        description TEXT,
                        status VARCHAR(50) DEFAULT 'open',
                        priority VARCHAR(20) DEFAULT 'medium',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        resolved_by VARCHAR(255),
                        resolution_notes TEXT
                    )
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tickets_customer 
                    ON support_tickets(customer_id)
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tickets_status 
                    ON support_tickets(status)
                """)
                logging.info("[DB] Support tickets table initialized")
        except Exception as e:
            logging.warning(f"[DB] Could not initialize support_tickets table: {e}")

    async def _initialize_session_storage_table(self):
        """Create table for persistent session storage"""
        if not self.pool:
            return
        
        try:
            async with self.pool.acquire() as connection:
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_sessions (
                        session_id VARCHAR(255) PRIMARY KEY,
                        customer_id INTEGER,
                        data JSONB,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logging.info("[DB] Session storage table initialized")
        except Exception as e:
            logging.warning(f"[DB] Could not initialize session storage table: {e}")
    
    async def _initialize_sentiment_alerts_table(self):
        """Create sentiment_alerts table for tracking unhappy customers"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as connection:
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS sentiment_alerts (
                        id SERIAL PRIMARY KEY,
                        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
                        customer_name VARCHAR(255),
                        customer_email VARCHAR(255),
                        sentiment_score DECIMAL(3,2),
                        sentiment_label VARCHAR(50),
                        trigger_phrases TEXT,
                        customer_message TEXT,
                        context_summary TEXT,
                        churn_risk VARCHAR(20),
                        recommended_action TEXT,
                        session_id VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        resolved_by VARCHAR(255),
                        resolution_notes TEXT
                    )
                """)
                
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sentiment_alerts_risk 
                    ON sentiment_alerts(churn_risk)
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sentiment_alerts_customer 
                    ON sentiment_alerts(customer_id)
                """)
                
                # Clear old alerts on startup (keep only last 7 days)
                await connection.execute("""
                    DELETE FROM sentiment_alerts 
                    WHERE resolved_at IS NOT NULL
                    AND resolved_at < NOW() - INTERVAL '7 days'
                """)
                
                logging.info("[DB] Sentiment alerts table initialized")
                
        except Exception as e:
            logging.warning(f"[DB] Could not initialize sentiment_alerts table: {e}")
    
    async def disconnect(self):
        # Only close the pool if we created it (owns_pool is True)
        if self.pool and self._owns_pool:
            await self.pool.close()
            self.pool = None
            logging.info("Database pool closed (owned)")
    
    async def execute_query(self, query, params=None):
        if not self.pool:
            if not await self.connect():
                return None, "Database connection failed"
        try:
            # Replace %s with $1, $2, etc. for asyncpg
            converted_query = query
            if params:
                for i in range(len(params)):
                    converted_query = converted_query.replace('%s', f'${i+1}', 1)
            
            async with self.pool.acquire() as connection:
                if params:
                    results = await connection.fetch(converted_query, *params)
                else:
                    results = await connection.fetch(converted_query)
                # Convert Record objects to dicts
                return [dict(r) for r in results], None
        except Exception as e:
            logging.error(f"Query execution failed: {e}")
            return None, str(e)
    
    async def find_customer_by_pin(self, pin):
        """Find customer by PIN code"""
        query = "SELECT id, name, email, phone, pin FROM customers WHERE pin = %s"
        results, error = await self.execute_query(query, (pin,))
        if results and len(results) > 0:
            return results[0]
        return None
    
    async def run_predefined_query(self, query_type, customer_id):
        """Run a predefined query safely"""
        if query_type not in PREDEFINED_QUERIES:
            return None, f"Unknown query type: {query_type}"
        
        query = PREDEFINED_QUERIES[query_type]
        return await self.execute_query(query, (customer_id,))
    
    async def get_plan_by_name(self, plan_name):
        """Get plan details by name (case insensitive partial match)"""
        query = """
            SELECT id, name, price, data_limit_gb, support_level, features
            FROM plans
            WHERE LOWER(name) LIKE LOWER(%s)
            ORDER BY price ASC
            LIMIT 1
        """
        results, error = await self.execute_query(query, (f"%{plan_name}%",))
        if results and len(results) > 0:
            return results[0]
        return None
    
    async def create_upgrade_request(self, customer_id, customer_name, customer_email, 
                                current_plan_id, current_plan_name,
                                target_plan_id, target_plan_name, session_id=None):
        """Create an upgrade request in the database"""
        
        # Check for existing pending request for same customer and plan
        check_query = """
            SELECT id FROM upgrade_requests 
            WHERE customer_id = %s AND target_plan_name = %s AND status = 'pending'
        """
        existing, _ = await self.execute_query(check_query, (customer_id, target_plan_name))
        if existing and len(existing) > 0:
            logging.info(f"[DB] Upgrade request already exists for customer {customer_id}")
            return existing[0]['id'], None
        
        # Insert new request
        insert_query = """
            INSERT INTO upgrade_requests 
            (customer_id, customer_name, customer_email, current_plan_id, current_plan_name,
             target_plan_id, target_plan_name, status, priority, notes, session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 'normal', 'Created via voice agent', %s)
            RETURNING id
        """
        
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
                    
            # Replace %s with $1, $2, etc.
            converted_query = insert_query
            params = (
                customer_id, customer_name, customer_email,
                current_plan_id, current_plan_name,
                target_plan_id, target_plan_name, session_id
            )
            for i in range(len(params)):
                converted_query = converted_query.replace('%s', f'${i+1}', 1)
                
            async with self.pool.acquire() as connection:
                result = await connection.fetchval(converted_query, *params)
                
                request_id = result
                logging.info(f"[DB] Created upgrade request #{request_id}: {customer_name} -> {target_plan_name}")
                return request_id, None
                
        except Exception as e:
            logging.error(f"[DB] Failed to create upgrade request: {e}")
            return None, str(e)

    async def create_ticket(self, customer_id, subject, description, priority='medium'):
        """Create a new support ticket"""
        insert_query = """
            INSERT INTO support_tickets (customer_id, subject, description, status, priority, created_at)
            VALUES (%s, %s, %s, 'open', %s, NOW())
            RETURNING id
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            # Replace %s with $1, $2, etc.
            converted_query = insert_query
            params = (customer_id, subject, description, priority)
            for i in range(len(params)):
                converted_query = converted_query.replace('%s', f'${i+1}', 1)
            
            async with self.pool.acquire() as connection:
                ticket_id = await connection.fetchval(converted_query, *params)
                logging.info(f"[DB] Created ticket #{ticket_id} for customer {customer_id}")
                return ticket_id, None
        except Exception as e:
            logging.error(f"[DB] Failed to create ticket: {e}")
            return None, str(e)

    async def close_ticket_by_id(self, customer_id, ticket_id):
        """Close a specific ticket by ID"""
        update_query = """
            UPDATE support_tickets 
            SET status = 'resolved', resolved_at = NOW()
            WHERE id = $1 AND customer_id = $2
            RETURNING id, subject
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            async with self.pool.acquire() as connection:
                # Need integer ID
                try:
                    t_id = int(ticket_id)
                except:
                    return None, "Invalid ticket ID"
                    
                result = await connection.fetchrow(update_query, t_id, customer_id)
                
                if result:
                    logging.info(f"[DB] Closed ticket #{result['id']} for customer {customer_id}")
                    return (result['id'], result['subject']), None
                else:
                    return None, "Ticket not found or already closed"
        except Exception as e:
            logging.error(f"[DB] Failed to close ticket {ticket_id}: {e}")
            return None, str(e)

    async def get_open_tickets(self, customer_id):
        """Get all open tickets for a customer"""
        query = """
            SELECT id, subject, created_at 
            FROM support_tickets 
            WHERE customer_id = $1 AND status = 'open'
            ORDER BY created_at DESC
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            async with self.pool.acquire() as connection:
                results = await connection.fetch(query, customer_id)
                return [dict(r) for r in results], None
        except Exception as e:
            logging.error(f"[DB] Failed to fetch open tickets: {e}")
            return None, str(e)

    async def close_latest_ticket(self, customer_id):
        """Close the most recent open ticket for a customer"""
        update_query = """
            UPDATE support_tickets 
            SET status = 'resolved', resolved_at = NOW()
            WHERE id = (
                SELECT id FROM support_tickets 
                WHERE customer_id = $1 AND status = 'open' 
                ORDER BY created_at DESC LIMIT 1
            )
            RETURNING id, subject
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            async with self.pool.acquire() as connection:
                result = await connection.fetchrow(update_query, customer_id)
                
                if result:
                    logging.info(f"[DB] Closed ticket #{result['id']} for customer {customer_id}")
                    return (result['id'], result['subject']), None
                else:
                    return None, "No open tickets found"
        except Exception as e:
            logging.error(f"[DB] Failed to close ticket: {e}")
            return None, str(e)
    
    async def create_sentiment_alert(self, customer_id, customer_name, customer_email,
                               sentiment_score, sentiment_label, trigger_phrases,
                               customer_message, context_summary, churn_risk,
                               recommended_action, session_id=None):
        """Create a sentiment alert for unhappy customer"""
        
        insert_query = """
            INSERT INTO sentiment_alerts 
            (customer_id, customer_name, customer_email, sentiment_score, sentiment_label,
             trigger_phrases, customer_message, context_summary, churn_risk,
             recommended_action, session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        
        try:
            if not self.pool:
                if not await self.connect():
                    return None, "Database connection failed"
            
            # Replace %s with $1, $2, etc.
            converted_query = insert_query
            params = (
                customer_id, customer_name, customer_email,
                sentiment_score, sentiment_label, trigger_phrases,
                customer_message, context_summary, churn_risk,
                recommended_action, session_id
            )
            for i in range(len(params)):
                converted_query = converted_query.replace('%s', f'${i+1}', 1)
            
            async with self.pool.acquire() as connection:
                alert_id = await connection.fetchval(converted_query, *params)
                logging.warning(f"[SENTIMENT] ⚠️ Alert #{alert_id}: {sentiment_label} customer - {customer_name} ({churn_risk} churn risk)")
                return alert_id, None
                
        except Exception as e:
            logging.error(f"[SENTIMENT] Failed to create alert: {e}")
            return None, str(e)

    async def save_session(self, session_id, customer_id, data):
        """Save conversation session to database"""
        query = """
            INSERT INTO conversation_sessions (session_id, customer_id, data, last_updated)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (session_id) 
            DO UPDATE SET customer_id = $2, data = $3, last_updated = NOW()
        """
        try:
            if not self.pool:
                if not await self.connect():
                    return False
            
            async with self.pool.acquire() as connection:
                await connection.execute(query, session_id, customer_id, json.dumps(data))
                return True
        except Exception as e:
            logging.error(f"[DB] Failed to save session: {e}")
            return False

    async def load_session(self, session_id):
        """Load conversation session from database"""
        query = "SELECT data FROM conversation_sessions WHERE session_id = $1"
        try:
            if not self.pool:
                if not await self.connect():
                    return None
            
            async with self.pool.acquire() as connection:
                result = await connection.fetchval(query, session_id)
                if result:
                    return json.loads(result)
                return None
        except Exception as e:
            logging.error(f"[DB] Failed to load session: {e}")
            return None


# =============================================================================
# TEXT PROCESSING UTILITIES
# =============================================================================
def clean_tts_text(text):
    """Clean text for TTS - remove problematic characters"""
    if not text:
        return None
    
    text = text.strip().strip('"\'""''')
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)  # Italic
    text = re.sub(r'`([^`]+)`', r'\1', text)  # Code
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    
    # Check if text contains valid characters for any supported language
    # Latin, Hebrew, Cyrillic, Arabic, CJK (Chinese/Japanese/Korean)
    valid_chars = r'[a-zA-Z0-9\u0590-\u05FF\u0400-\u04FF\u0600-\u06FF\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]'
    
    if not text or not re.search(valid_chars, text):
        return None
    
    return text.strip()


# =============================================================================
# SENTIMENT ANALYSIS - Detect unhappy customers at risk of churning
# =============================================================================
NEGATIVE_INDICATORS = {
    # English
    'cancel': 0.9, 'cancelling': 0.9, 'cancellation': 0.9, 'terminate': 0.9, 'leave': 0.8, 'switch to': 0.75,
    'terrible': 0.7, 'awful': 0.7, 'horrible': 0.75, 'worst': 0.8, 'angry': 0.7, 'furious': 0.8,
    'not working': 0.5, 'broken': 0.55, 'problem': 0.4, 'issue': 0.35, 'disappointed': 0.55,
    
    # Hebrew
    'לבטל': 0.9, 'ביטול': 0.9, 'מבטל': 0.9, 'לעזוב': 0.8, 'להתנתק': 0.9,
    'נורא': 0.7, 'איום': 0.75, 'גרוע': 0.7, 'כועס': 0.7, 'זועם': 0.8,
    'לא עובד': 0.5, 'שבור': 0.55, 'תקלה': 0.45, 'בעיה': 0.4, 'מאוכזב': 0.55,
    
    # Spanish
    'cancelar': 0.9, 'terminar': 0.9, 'dar de baja': 0.9, 'irme': 0.8, 'cambiar de': 0.75,
    'terrible': 0.7, 'horrible': 0.75, 'peor': 0.8, 'enfadado': 0.7, 'furioso': 0.8,
    'no funciona': 0.5, 'roto': 0.55, 'problema': 0.4, 'decepcionado': 0.55,
    
    # French
    'annuler': 0.9, 'résilier': 0.9, 'quitter': 0.8, 'changer de': 0.75,
    'terrible': 0.7, 'horrible': 0.75, 'pire': 0.8, 'colère': 0.7, 'furieux': 0.8,
    'ne marche pas': 0.5, 'cassé': 0.55, 'problème': 0.4, 'déçu': 0.55,
    
    # German
    'kündigen': 0.9, 'stornieren': 0.9, 'verlassen': 0.8, 'wechseln': 0.75,
    'schrecklich': 0.7, 'furchtbar': 0.75, 'schlimmste': 0.8, 'wütend': 0.7,
    'funktioniert nicht': 0.5, 'kaputt': 0.55, 'problem': 0.4, 'enttäuscht': 0.55,
    
    # Russian
    'отменить': 0.9, 'расторгнуть': 0.9, 'уйти': 0.8, 'сменить': 0.75,
    'ужасно': 0.7, 'кошмар': 0.75, 'худший': 0.8, 'злой': 0.7,
    'не работает': 0.5, 'сломан': 0.55, 'проблема': 0.4, 'разочарован': 0.55,
    
    # Arabic
    'إلغاء': 0.9, 'إنهاء': 0.9, 'مغادرة': 0.8, 'تغيير': 0.75,
    'فظيع': 0.7, 'مريع': 0.75, 'أسوأ': 0.8, 'غاضب': 0.7,
    'لا يعمل': 0.5, 'مكسور': 0.55, 'مشكلة': 0.4, 'محبط': 0.55,
    
    # Portuguese
    'cancelar': 0.9, 'encerrar': 0.9, 'sair': 0.8, 'mudar': 0.75,
    'terrível': 0.7, 'horrível': 0.75, 'pior': 0.8, 'bravo': 0.7, 'furioso': 0.8,
    'não funciona': 0.5, 'quebrado': 0.55, 'problema': 0.4, 'decepcionado': 0.55,
    
    # Italian
    'cancellare': 0.9, 'terminare': 0.9, 'lasciare': 0.8, 'cambiare': 0.75,
    'terribile': 0.7, 'orribile': 0.75, 'peggiore': 0.8, 'arrabbiato': 0.7,
    'non funziona': 0.5, 'rotto': 0.55, 'problema': 0.4, 'deluso': 0.55
}

POSITIVE_INDICATORS = {
    # English
    'thank': 0.3, 'thanks': 0.3, 'appreciate': 0.4, 'great': 0.4, 'excellent': 0.5, 'amazing': 0.5,
    'helpful': 0.4, 'perfect': 0.45, 'love': 0.5, 'happy': 0.4, 'satisfied': 0.4,
    
    # Hebrew
    'תודה': 0.3, 'מעולה': 0.5, 'נהדר': 0.45, 'מושלם': 0.45, 'אוהב': 0.5, 'מרוצה': 0.4, 'עוזר': 0.4,
    
    # Spanish
    'gracias': 0.3, 'agradezco': 0.4, 'genial': 0.4, 'excelente': 0.5, 'increíble': 0.5,
    'útil': 0.4, 'perfecto': 0.45, 'encanta': 0.5, 'feliz': 0.4, 'satisfecho': 0.4,
    
    # French
    'merci': 0.3, 'apprécie': 0.4, 'super': 0.4, 'excellent': 0.5, 'incroyable': 0.5,
    'utile': 0.4, 'parfait': 0.45, 'adore': 0.5, 'content': 0.4, 'satisfait': 0.4,
    
    # German
    'danke': 0.3, 'super': 0.4, 'ausgezeichnet': 0.5, 'toll': 0.5, 'hilfreich': 0.4,
    'perfekt': 0.45, 'liebe': 0.5, 'froh': 0.4, 'zufrieden': 0.4,
    
    # Russian
    'спасибо': 0.3, 'благодарю': 0.4, 'отлично': 0.5, 'прекрасно': 0.5, 'полезно': 0.4,
    'идеально': 0.45, 'люблю': 0.5, 'рад': 0.4, 'доволен': 0.4,
    
    # Arabic
    'شكرا': 0.3, 'أقدر': 0.4, 'رائع': 0.4, 'ممتاز': 0.5, 'مذهل': 0.5, 'مفيد': 0.4,
    'مثالي': 0.45, 'أحب': 0.5, 'سعيد': 0.4, 'راض': 0.4,
    
    # Portuguese
    'obrigado': 0.3, 'agradeço': 0.4, 'ótimo': 0.4, 'excelente': 0.5, 'incrível': 0.5,
    'útil': 0.4, 'perfeito': 0.45, 'amo': 0.5, 'feliz': 0.4, 'satisfeito': 0.4,
    
    # Italian
    'grazie': 0.3, 'apprezzo': 0.4, 'grande': 0.4, 'eccellente': 0.5, 'fantastico': 0.5,
    'utile': 0.4, 'perfetto': 0.45, 'amo': 0.5, 'felice': 0.4, 'soddisfatto': 0.4
}

def analyze_sentiment(text, language='en', conversation_history=None):
    """
    Analyze customer sentiment from their message (multi-language support).
    Returns: dict with score (-1 to 1), label, trigger_phrases, churn_risk, recommended_action
    """
    if not text:
        return None
    
    text_lower = text.lower()
    
    # Calculate scores
    negative_score = 0.0
    positive_score = 0.0
    trigger_phrases = []
    
    for phrase, weight in NEGATIVE_INDICATORS.items():
        if phrase in text_lower:
            negative_score += weight
            trigger_phrases.append(phrase)
    
    for phrase, weight in POSITIVE_INDICATORS.items():
        if phrase in text_lower:
            positive_score += weight
    
    # Normalize scores
    negative_score = min(negative_score, 1.0)
    positive_score = min(positive_score, 1.0)
    
    # Final sentiment score (-1 = very negative, 0 = neutral, 1 = very positive)
    sentiment_score = positive_score - negative_score
    
    # Determine label and churn risk
    if sentiment_score <= -0.6:
        label = "very_negative"
        churn_risk = "high"
        action = "URGENT: Immediate callback by senior agent. Offer compensation/discount."
    elif sentiment_score <= -0.35:
        label = "negative"
        churn_risk = "medium"
        action = "Priority callback within 24h. Address specific complaints."
    elif sentiment_score <= -0.15:
        label = "slightly_negative"
        churn_risk = "low"
        action = "Monitor conversation. Follow up if issues persist."
    elif sentiment_score >= 0.3:
        label = "positive"
        churn_risk = "none"
        action = None
    else:
        label = "neutral"
        churn_risk = "none"
        action = None
    
    # Check for explicit cancellation intent (always high risk)
    cancel_words = ['cancel', 'terminate', 'unsubscribe', 'לבטל', 'ביטול', 'להתנתק']
    for word in cancel_words:
        if word in text_lower:
            churn_risk = "high"
            label = "cancellation_intent"
            action = "URGENT: Customer wants to cancel. Immediate retention call required."
            break
    
    return {
        'score': round(sentiment_score, 2),
        'label': label,
        'trigger_phrases': trigger_phrases,
        'churn_risk': churn_risk,
        'recommended_action': action,
        'needs_alert': churn_risk in ['high', 'medium']
    }


def detect_query_type(text):
    """Detect what type of information the user is asking about"""
    text_lower = text.lower()
    
    for query_type, triggers in QUERY_TRIGGERS.items():
        for trigger in triggers:
            if trigger in text_lower:
                return query_type
    
    return None


def extract_plan_choice(text):
    """Extract which plan the user wants from their response"""
    text_lower = text.lower()
    
    # Common plan name patterns
    if any(word in text_lower for word in ['premium', 'פרימיום', 'הכי טוב', 'best', 'top', 'highest']):
        return 'premium'
    elif any(word in text_lower for word in ['standard', 'סטנדרט', 'רגיל', 'regular', 'basic', 'normal']):
        return 'standard'
    elif any(word in text_lower for word in ['pro', 'professional', 'business']):
        return 'pro'
    
    return None


def is_confirmation(text):
    """Check if user is confirming something"""
    text_lower = text.lower().strip()
    confirmations = ['yes', 'yeah', 'yep', 'sure', 'ok', 'okay', 'correct', 'right', 
                     'confirm', 'confirmed', 'absolutely', 'definitely', 'go ahead',
                     'כן', 'בטח', 'אישור', 'נכון', 'קדימה']
    return any(word in text_lower for word in confirmations)


def is_cancellation(text):
    """Check if user is cancelling something"""
    text_lower = text.lower().strip()
    cancellations = ['no', 'nope', 'cancel', 'stop', 'never mind', 'forget it',
                     'don\'t', 'dont', 'not', 'wait', 'hold on',
                     'לא', 'ביטול', 'עזוב', 'תשכח']
    return any(word in text_lower for word in cancellations)


# =============================================================================
# RESPONSE FORMATTERS - Natural language for voice (multi-language)
# =============================================================================
def format_query_results(query_type, results, customer_name=None, lang="en"):
    """Format database results into natural conversational language"""
    name = customer_name.split()[0] if customer_name else ""  # First name only
    
    if not results or len(results) == 0:
        return get_natural_response('info_not_found', lang=lang, type=query_type)
    
    if query_type == 'subscription':
        r = results[0]
        status = r.get('status', 'unknown')
        plan = r.get('plan_name', 'your plan')
        price = r.get('price', 0)
        auto_renew = "on" if r.get('auto_renew') else "off"
        
        if status == 'active':
            return f"You're on the {plan} plan, {name}. That's {price} dollars a month with auto-renewal {auto_renew}."
        else:
            return f"Your {plan} subscription is currently {status}."
    
    elif query_type == 'balance':
        r = results[0]
        pending = float(r.get('pending_amount', 0))
        overdue = float(r.get('overdue_amount', 0))
        
        if overdue > 0:
            return f"Looks like you have {overdue:.2f} dollars overdue, {name}. Want me to help you sort that out?"
        elif pending > 0:
            return f"You've got {pending:.2f} dollars pending, {name}. Nothing overdue though!"
        else:
            return f"Good news, {name}! Your balance is all clear."
    
    elif query_type == 'invoices':
        count = len(results)
        latest = results[0]
        amount = latest.get('amount', 0)
        status = latest.get('status', 'unknown')
        
        if count == 1:
            return f"You've got one invoice for {amount} dollars, and it's {status}."
        else:
            return f"I see {count} invoices. Your most recent one is {amount} dollars, currently {status}."
    
    elif query_type == 'plan':
        r = results[0]
        name_plan = r.get('name', 'your plan')
        price = r.get('price', 0)
        data = r.get('data_limit_gb', 'unlimited')
        support = r.get('support_level', 'standard')
        
        return f"You're on {name_plan} at {price} dollars per month. You've got {data} gigs of data and {support} level support."
    
    elif query_type == 'tickets':
        open_tickets = [t for t in results if t.get('status') == 'open']
        if open_tickets:
            subject = open_tickets[0].get('subject', 'an open issue')
            return f"You have {len(open_tickets)} open ticket{'s' if len(open_tickets) > 1 else ''}, {name}. The most recent is about {subject}."
        elif results:
            return f"Good news - all your {len(results)} support tickets have been resolved!"
        return "You don't have any support tickets on file."
    
    elif query_type == 'customer_info':
        r = results[0]
        email = r.get('email', 'not set')
        phone = r.get('phone', 'not set')
        return f"Your account email is {email} and phone is {phone}."
    
    return "Here's what I found for you."


def generate_card_payload(query_type, results):
    """Generate rich UI card payload based on query results"""
    if not results:
        return None
    
    card_data = None
    
    if query_type == 'subscription':
        r = results[0]
        card_data = {
            "title": "Subscription Details",
            "items": [
                {"label": "Plan", "value": r.get('plan_name')},
                {"label": "Status", "value": r.get('status').title()},
                {"label": "Price", "value": f"${r.get('price')}/mo"},
                {"label": "Renews", "value": "Yes" if r.get('auto_renew') else "No"}
            ],
            "type": "info"
        }
        
    elif query_type == 'balance':
        r = results[0]
        card_data = {
            "title": "Account Balance",
            "items": [
                {"label": "Pending", "value": f"${float(r.get('pending_amount', 0)):.2f}"},
                {"label": "Overdue", "value": f"${float(r.get('overdue_amount', 0)):.2f}", "highlight": float(r.get('overdue_amount', 0)) > 0},
                {"label": "Paid", "value": f"${float(r.get('paid_amount', 0)):.2f}"}
            ],
            "type": "alert" if float(r.get('overdue_amount', 0)) > 0 else "success"
        }
        
    elif query_type == 'invoices':
        # Show list of recent invoices
        items = []
        for inv in results[:3]: # Top 3
            items.append({
                "label": f"Inv #{inv.get('id')} ({inv.get('status')})",
                "value": f"${inv.get('amount')}"
            })
        
        card_data = {
            "title": "Recent Invoices",
            "items": items,
            "type": "list"
        }
        
    elif query_type == 'tickets':
        # Show list of recent tickets
        items = []
        for t in results[:3]:
            items.append({
                "label": f"#{t.get('id')} {t.get('subject')}",
                "value": t.get('status').upper(),
                "highlight": t.get('status') == 'open'
            })
            
        card_data = {
            "title": "Support Tickets",
            "items": items,
            "type": "list"
        }
    
    if card_data:
        return {
            "type": "visual_card",
            "card_type": query_type,
            "data": card_data
        }
    return None


# =============================================================================
# MULTI-LANGUAGE NUMBER PARSING - Extract PIN from spoken words
# =============================================================================
def parse_spoken_numbers(text: str, language: str = 'en') -> str:
    """
    Convert spoken number words to digits in any supported language.
    Example: "one two three four" -> "1234"
    Example (Hebrew): "אחת שתיים שלוש ארבע" -> "1234"
    """
    if not text:
        return ""
    
    # Get language code (handle locale codes like 'en-US')
    lang_code = language.split('-')[0].lower() if '-' in language else language.lower()
    
    # Get number words for this language, fallback to English
    number_words = NUMBER_WORDS.get(lang_code, NUMBER_WORDS.get('en', {}))
    
    # Also always include English numbers as fallback (many people mix languages)
    all_number_words = {**number_words, **NUMBER_WORDS.get('en', {})}
    
    result = []
    text_lower = text.lower()
    
    # First, extract any actual digits
    for char in text:
        if char.isdigit():
            result.append(char)
    
    # Then, look for number words
    for word, digit in all_number_words.items():
        if word.lower() in text_lower:
            # Count occurrences
            count = text_lower.count(word.lower())
            for _ in range(count):
                result.append(digit)
    
    # If we found digits, return them (limit to reasonable PIN length)
    if result:
        return ''.join(result[:10])  # Max 10 digits
    
    return ""


def extract_pin_from_text(text: str, language: str = 'en') -> str:
    """
    Extract a 4-digit PIN from spoken text.
    Handles both digits and spoken number words in multiple languages.
    """
    # First try direct digit extraction
    digits = ''.join(c for c in text if c.isdigit())
    if len(digits) >= 4:
        return digits[:4]
    
    # Then try parsing spoken numbers
    parsed = parse_spoken_numbers(text, language)
    if len(parsed) >= 4:
        return parsed[:4]
    
    # Combine both approaches
    all_digits = digits + parsed
    if len(all_digits) >= 4:
        return all_digits[:4]
    
    return ""


# =============================================================================
# RETENTION RESPONSE - Generate empathetic responses for frustrated customers
# =============================================================================
def get_retention_response(sentiment_result: dict, language: str = 'en') -> str:
    """
    Generate an appropriate retention response based on sentiment analysis.
    """
    churn_risk = sentiment_result.get('churn_risk', 'low')
    
    # Retention responses by language and risk level
    responses = {
        'en': {
            'high': "I can hear that you're really frustrated, and I completely understand. Let me personally make sure we resolve this for you today. I'm also going to flag this to our customer success team for a follow-up. What would make this right for you?",
            'medium': "I apologize for any inconvenience. Your satisfaction is really important to us. Let me see what I can do to help make this better.",
        },
        'he': {
            'high': "אני שומע שאתה מאוד מתוסכל, ואני מבין לגמרי. אני אדאג אישית שנפתור את זה היום. מה יכול לעזור לך?",
            'medium': "אני מתנצל על אי הנוחות. שביעות הרצון שלך חשובה לנו מאוד. בוא נראה איך אני יכול לעזור.",
        },
        'es': {
            'high': "Entiendo completamente su frustración. Permítame asegurarme personalmente de resolver esto hoy. ¿Qué podemos hacer para mejorar la situación?",
            'medium': "Lamento las molestias. Su satisfacción es muy importante para nosotros.",
        },
    }
    
    lang_code = language.split('-')[0].lower() if '-' in language else language.lower()
    lang_responses = responses.get(lang_code, responses.get('en'))
    
    if churn_risk == 'high':
        return lang_responses.get('high', lang_responses.get('medium', ''))
    elif churn_risk == 'medium':
        return lang_responses.get('medium', '')
    
    return ""


# =============================================================================
# HEALTH CHECK - Check all services status
# =============================================================================
async def check_services_health() -> dict:
    """
    Check health of all connected services (ASR, TTS, LLM, DB).
    Returns comprehensive health status.
    """
    status = {
        "healthy": True,
        "timestamp": datetime.now().isoformat(),
        "services": {},
        "circuit_breakers": {},
        "watchdog_issues": [],
    }
    
    client = await get_http_client()
    
    # Check ASR
    try:
        asr_url = DEFAULT_SETTINGS.get('asr_server_address', '')
        if asr_url:
            if not asr_url.startswith('http'):
                asr_url = f"http://{asr_url}"
            resp = await client.get(f"{asr_url}/health", timeout=5)
            status["services"]["asr"] = {"status": "healthy" if resp.status_code == 200 else "unhealthy"}
        else:
            status["services"]["asr"] = {"status": "not_configured"}
    except Exception as e:
        status["services"]["asr"] = {"status": "unhealthy", "error": str(e)[:100]}
        status["healthy"] = False
    
    # Check TTS
    try:
        tts_url = DEFAULT_SETTINGS.get('tts_server_address', '')
        if tts_url:
            if not tts_url.startswith('http'):
                tts_url = f"http://{tts_url}"
            resp = await client.get(f"{tts_url}/health", timeout=5)
            status["services"]["tts"] = {"status": "healthy" if resp.status_code == 200 else "unhealthy"}
        else:
            status["services"]["tts"] = {"status": "not_configured"}
    except Exception as e:
        status["services"]["tts"] = {"status": "unhealthy", "error": str(e)[:100]}
        status["healthy"] = False
    
    # Check Database
    try:
        db_pool = await ensure_db_pool()
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            status["services"]["database"] = {"status": "healthy"}
        else:
            status["services"]["database"] = {"status": "not_configured"}
    except Exception as e:
        status["services"]["database"] = {"status": "unhealthy", "error": str(e)[:100]}
    
    # Add circuit breaker status
    status["circuit_breakers"] = {
        "asr": _asr_circuit.get_status(),
        "tts": _tts_circuit.get_status(),
        "llm": _llm_circuit.get_status(),
    }
    
    # Add watchdog issues
    status["watchdog_issues"] = _watchdog.check_health()
    if status["watchdog_issues"]:
        status["healthy"] = False
    
    return status


# =============================================================================
# AUDIO PROCESSING
# =============================================================================
def inspect_wav_properties(file_input) -> tuple[int, int, str]:
    try:
        # Handle both filepath and file-like object
        if isinstance(file_input, str):
            if not os.path.exists(file_input):
                return 0, 0, f"Error: File not found: {file_input}"
            f = wave.open(file_input, 'rb')
        else:
            f = wave.open(file_input, 'rb')
            
        with f as wf:
            rate, channels = wf.getframerate(), wf.getnchannels()
            status = f"Audio Properties - Rate: {rate}Hz, Channels: {channels}, Width: {wf.getsampwidth()*8}-bit"
            return rate, channels, status
    except wave.Error as e:
        return 0, 0, f"Error inspecting WAV file: {e}"
    except Exception as e:
        return 0, 0, f"Error processing audio: {e}"


async def resample_audio_async(input_file: str, target_sample_rate: int = 16000) -> tuple[str, bool]:
    """Resample audio to target sample rate using ffmpeg (async)."""
    output_file = input_file.replace('.wav', f'_{target_sample_rate}.wav')
    try:
        cmd = [
            'ffmpeg', '-y', '-i', input_file,
            '-ar', str(target_sample_rate),
            '-ac', '1',  # mono
            '-loglevel', 'error',
            output_file
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            logging.info(f"Resampled audio to {target_sample_rate}Hz: {output_file}")
            return output_file, True
        else:
            logging.error(f"Failed to resample audio: {stderr.decode()}")
            return input_file, False
            
    except FileNotFoundError:
        logging.warning("ffmpeg not found, using original audio")
        return input_file, False
    except Exception as e:
        logging.error(f"Resample error: {e}")
        return input_file, False


# =============================================================================
# WHISPER ASR
# =============================================================================
async def transcribe_with_whisper(audio_input, settings: dict, max_retries: int = 2) -> str:
    """Transcribe audio using Whisper server (HTTP) with retry logic and circuit breaker"""
    
    # Check circuit breaker first
    if not _asr_circuit.can_execute():
        logging.warning("[ASR] Circuit breaker is OPEN, skipping request")
        return ""
    
    asr_server = settings.get('asr_server_address', '').strip()
    language = settings.get('asr_language_code', 'en')
    
    if not asr_server:
        logging.error("[ASR] No ASR server address configured")
        return ""
    
    if isinstance(audio_input, bytes):
        audio_data = audio_input
    else:
        with open(audio_input, 'rb') as f:
            audio_data = f.read()
    
    asr_server = asr_server.strip()
    asr_server = ' '.join(asr_server.split())
    
    if not asr_server.startswith('http'):
        asr_url = f"http://{asr_server}"
    else:
        asr_url = asr_server
    
    asr_url = asr_url.replace('http:// ', 'http://').replace('https:// ', 'https://')
    
    # Use global LOCALE_MAP (defined at top of file for performance)
    is_auto = not language or language == "auto" or "auto" in str(language).lower()
    
    if is_auto:
        full_language = 'en-US'
    elif '-' in language:
        full_language = language
    else:
        short_code = language.lower().strip()
        full_language = LOCALE_MAP.get(short_code, f"{short_code}-{short_code.upper()}")
    
    logging.info(f"[ASR] Sending to Whisper: {asr_url} (lang={full_language})")
    
    data = {'language': full_language}
    client = await get_http_client()
    
    for attempt in range(max_retries + 1):
        try:
            files = {'file': ('audio.wav', audio_data, 'audio/wav')}
            
            endpoints = [
                '/v1/audio/transcriptions',
                '/transcribe',
                '/asr',
                '/inference'
            ]
            
            for endpoint in endpoints:
                try:
                    response = await client.post(f"{asr_url}{endpoint}", files=files, data=data)
                    
                    if response.status_code == 200:
                        result = response.json()
                        if isinstance(result, dict):
                            transcript = result.get('text', result.get('transcription', result.get('transcript', '')))
                        else:
                            transcript = str(result)
                        logging.info(f"[ASR] Success via {endpoint}")
                        _asr_circuit.record_success()
                        _watchdog.record_asr_success()
                        return transcript.strip()
                except httpx.TimeoutException:
                    continue
                except Exception as e:
                    continue
            
            if attempt < max_retries:
                logging.warning(f"[ASR] All endpoints failed, retry {attempt + 1}/{max_retries}")
                await asyncio.sleep(0.5 * (attempt + 1))
            
        except Exception as e:
            logging.error(f"[ASR] Whisper error (attempt {attempt + 1}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    
    _asr_circuit.record_failure()
    logging.error(f"[ASR] All retries failed for {asr_url}")
    return ""


# =============================================================================
# XTTS TTS
# =============================================================================
async def synthesize_with_xtts(text: str, settings: dict, max_retries: int = 2) -> bytes:
    """Synthesize speech using XTTS server (HTTP) with retry logic, circuit breaker, and caching"""
    
    # Check circuit breaker first
    if not _tts_circuit.can_execute():
        logging.warning("[TTS] Circuit breaker is OPEN, skipping request")
        return b""
    
    tts_server = settings.get('tts_server_address', 'localhost:8000')
    language = settings.get('tts_language_code', 'en')
    
    # FIX v3.1.22: Use 'or' to handle empty string fallback (not just None)
    # UI sends 'tts_voice', backend default uses 'tts_speaker'
    speaker = settings.get('tts_speaker') or settings.get('tts_voice') or ''
    
    logging.info(f"[TTS] Settings - tts_voice: '{settings.get('tts_voice')}', tts_speaker: '{settings.get('tts_speaker')}'")
    logging.info(f"[TTS] Final speaker: '{speaker}'")
    
    # Normalize language code (preserve zh-cn)
    if language.lower() != 'zh-cn' and '-' in language:
        language = language.split('-')[0].lower()
    else:
        language = language.lower()
    
    # Check TTS cache for short common phrases
    if len(text) < 200:
        cache_key = f"tts:{hash(text)}:{language}:{speaker}"
        cached = _tts_cache.get(cache_key)
        if cached:
            logging.info(f"[TTS] Cache HIT for: {text[:30]}...")
            return cached
    else:
        cache_key = None
    
    if not tts_server.startswith('http'):
        tts_url = f"http://{tts_server}"
    else:
        tts_url = tts_server
    
    logging.info(f"[TTS] Synthesizing: {text[:50]}... (lang={language}, speaker={speaker})")
    
    client = await get_http_client()
    
    for attempt in range(max_retries + 1):
        try:
            payload = {
                "text": text,
                "language": language,
            }
            # FIX v3.1.21: Always send speaker if it has a value (don't skip "default")
            # Let the XTTS server decide what to do with "default"
            if speaker:
                payload["speaker"] = speaker
            
            # Add stability parameters for more consistent voice
            payload.update({
                "temperature": 0.75,  # Slightly lower for consistency (default often 0.8)
                "top_p": 0.85,       # Restrict probability mass for stability
                "top_k": 50,         # Limit vocabulary for stability
                "repetition_penalty": 1.2, # Prevent stuttering loops
                "speed": 1.0         # Normal speed
            })
            
            logging.info(f"[TTS] Sending payload: {payload}")
            
            # Use stream to handle potential protocol errors (h11) gracefully
            audio_buffer = bytearray()
            async with client.stream("POST", f"{tts_url}/tts/wav", json=payload) as response:
                if response.status_code == 200:
                    try:
                        async for chunk in response.aiter_bytes():
                            audio_buffer.extend(chunk)
                    except Exception as stream_err:
                        logging.warning(f"[TTS] Stream interrupted (protocol error?): {stream_err}")
                        # Often XTTS sends valid WAV but closes connection early/incorrectly
                        # If we have data, we proceed
                    
                    if len(audio_buffer) > 0:
                        audio_data = bytes(audio_buffer)
                        logging.info(f"[TTS] Success, {len(audio_data)} bytes")
                        _tts_circuit.record_success()
                        _watchdog.record_tts_success()
                        
                        # Cache the result for short texts
                        if cache_key and len(audio_data) < 500000:  # Don't cache >500KB
                            _tts_cache.set(cache_key, audio_data)
                        
                        return audio_data
                    else:
                         logging.warning("[TTS] Received empty audio body")
                else:
                    logging.warning(f"[TTS] Failed: {response.status_code}")
                    
            if attempt < max_retries:
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
                
        except httpx.TimeoutException:
            logging.warning(f"[TTS] Timeout (attempt {attempt + 1})")
            if attempt < max_retries:
                await asyncio.sleep(0.3 * (attempt + 1))
        except Exception as e:
            logging.error(f"[TTS] XTTS error (attempt {attempt + 1}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(0.3 * (attempt + 1))
    
    _tts_circuit.record_failure()
    return b""


async def stream_tts_for_sentence(websocket, text_to_synthesize, stream_start_time, metrics, settings: dict):
    """Synthesize and stream TTS for a sentence"""
    text_to_synthesize = clean_tts_text(text_to_synthesize)
    if not text_to_synthesize:
        return
    
    logging.info(f"[TTS Task] Synthesizing: \"{text_to_synthesize[:50]}...\"")
    
    try:
        audio_data = await synthesize_with_xtts(text_to_synthesize, settings)
        
        if audio_data:
            current_time = time.time()
            if metrics['llm_tts_metrics']['first_tts_chunk_time'] is None:
                metrics['llm_tts_metrics']['first_tts_chunk_time'] = current_time - stream_start_time
            
            # Stream audio in chunks
            chunk_size = 8192
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i + chunk_size]
                metrics['llm_tts_metrics']['tts_chunk_latencies'].append(time.time() - stream_start_time)
                await websocket.send(chunk)
                
    except Exception as e:
        logging.error(f"[TTS Task] Error: {e}")


# =============================================================================
# LLM PROMPTS - Optimized for natural voice conversation
# =============================================================================
# =============================================================================
# LANGUAGE INSTRUCTIONS FOR LLM - Ensure response in correct language
# =============================================================================
LANGUAGE_INSTRUCTIONS = {
    "en": "LANGUAGE: You MUST respond ONLY in English. Even if the user speaks Hebrew, Arabic, or another language - understand them but ALWAYS reply in natural, conversational English.",
    "es": "IDIOMA: DEBES responder SOLAMENTE en español. Aunque el usuario hable otro idioma, entiéndelo pero SIEMPRE responde en español natural y conversacional.",
    "fr": "LANGUE: Tu DOIS répondre UNIQUEMENT en français. Même si l'utilisateur parle une autre langue, comprends-le mais réponds TOUJOURS en français naturel et conversationnel.",
    "de": "SPRACHE: Du MUSST ausschließlich auf Deutsch antworten. Auch wenn der Benutzer eine andere Sprache spricht, verstehe ihn aber antworte IMMER in natürlichem Deutsch.",
    "it": "LINGUA: DEVI rispondere SOLO in italiano. Anche se l'utente parla un'altra lingua, capiscilo ma rispondi SEMPRE in italiano naturale.",
    "pt": "IDIOMA: Você DEVE responder APENAS em português. Mesmo que o usuário fale outro idioma, entenda-o mas responda SEMPRE em português natural.",
    "pl": "JĘZYK: MUSISZ odpowiadać TYLKO po polsku. Nawet jeśli użytkownik mówi w innym języku, zrozum go ale ZAWSZE odpowiadaj po polsku.",
    "tr": "DİL: SADECE Türkçe yanıt vermelisin. Kullanıcı başka bir dilde konuşsa bile, onu anla ama HER ZAMAN Türkçe yanıt ver.",
    "ru": "ЯЗЫК: Ты ДОЛЖЕН отвечать ТОЛЬКО на русском. Даже если пользователь говорит на другом языке, пойми его но ВСЕГДА отвечай на русском.",
    "nl": "TAAL: Je MOET ALLEEN in het Nederlands antwoorden. Ook als de gebruiker een andere taal spreekt, begrijp hem maar antwoord ALTIJD in het Nederlands.",
    "cs": "JAZYK: MUSÍŠ odpovídat POUZE česky. I když uživatel mluví jiným jazykem, porozuměj mu ale VŽDY odpovídej česky.",
    "ar": "اللغة: يجب أن تجيب باللغة العربية فقط. حتى لو تحدث المستخدم بلغة أخرى، افهمه لكن أجب دائماً بالعربية.",
    "zh-cn": "语言规则：你必须只用中文回答。即使用户说其他语言，也要理解他们但始终用中文回答。",
    "zh": "语言规则：你必须只用中文回答。即使用户说其他语言，也要理解他们但始终用中文回答。",
    "ja": "言語ルール：日本語のみで回答してください。ユーザーが他の言語を話しても、理解した上で必ず日本語で回答してください。",
    "hu": "NYELV: CSAK magyarul válaszolj. Még ha a felhasználó más nyelven beszél is, értsd meg de MINDIG magyarul válaszolj.",
    "ko": "언어 규칙: 한국어로만 답변해야 합니다. 사용자가 다른 언어로 말해도 이해하되 항상 한국어로 답변하세요.",
}

# =============================================================================
# ADAPTIVE PERSONA TEMPLATES - Adjusts tone based on sentiment
# =============================================================================
PERSONA_TEMPLATES = {
    "default": "You are a friendly, natural-sounding customer service agent on a live phone call.",
    "empathetic": "You are an empathetic, patient, and apologetic customer service agent. The customer seems frustrated, so prioritize validating their feelings, apologizing sincerely, and resolving their issue quickly.",
    "retention": "You are a senior retention specialist. The customer is at risk of leaving. Be extremely professional, empathetic, and empowered to solve issues. Your primary goal is to save this customer relationship.",
}

def get_language_instruction(lang_code):
    """Get language instruction for the LLM based on TTS output language"""
    # Normalize language code
    if '-' in lang_code:
        lang_code = lang_code.split('-')[0].lower()
    return LANGUAGE_INSTRUCTIONS.get(lang_code, LANGUAGE_INSTRUCTIONS.get('en'))


def build_llm_prompt(transcript, conversation, history_context="", response_lang="en", sentiment=None):
    """Build a natural conversation prompt for LLM with adaptive persona"""
    
    name = conversation.customer_name.split()[0] if conversation and conversation.customer_name else "friend"
    lang_instruction = get_language_instruction(response_lang)
    
    # Select persona based on sentiment
    persona = PERSONA_TEMPLATES["default"]
    if sentiment:
        if sentiment.get('churn_risk') in ['high', 'medium'] or sentiment.get('label') == 'cancellation_intent':
            persona = PERSONA_TEMPLATES["retention"]
            logging.info("[Persona] Using RETENTION persona")
        elif sentiment.get('label') in ['negative', 'very_negative']:
            persona = PERSONA_TEMPLATES["empathetic"]
            logging.info("[Persona] Using EMPATHETIC persona")
    
    prompt = f'''{persona}

{lang_instruction}

CRITICAL VOICE RULES (this will be spoken aloud by TTS):
1. Use ONLY 1-3 short sentences - people can't follow long spoken responses
2. Use contractions naturally (I'm, you're, don't, can't, we'll)
3. Sound human: use "um", "well", "actually" sparingly but naturally
4. Never use bullet points, lists, markdown, or formatting
5. Reference the customer by first name ({name}) occasionally
6. Be warm but efficient - like a helpful friend who works at the company
7. If you don't understand, ask naturally: "Sorry, I didn't quite catch that - could you say that again?"
8. The customer may speak a DIFFERENT language (like Hebrew) - understand them but respond in YOUR designated language only

{f"Conversation so far:{chr(10)}{history_context}{chr(10)}" if history_context else ""}

{name} just said: "{transcript}"

Respond naturally in 1-3 sentences as if speaking on the phone:'''

    return prompt


def build_general_llm_prompt(transcript, history_context="", custom_template=None, response_lang="en"):
    """Build prompt for non-database mode conversations"""
    
    lang_instruction = get_language_instruction(response_lang)
    
    if custom_template and '{transcript}' in custom_template:
        # Add language instruction to custom template
        return f"{lang_instruction}\n\n{custom_template.format(transcript=transcript)}"
    
    prompt = f'''You're having a friendly voice conversation. Keep it natural and brief.

{lang_instruction}

VOICE RULES:
- Maximum 2-3 sentences - this is spoken, not written
- Use contractions and casual language
- Sound like a real person, not an AI
- No bullet points or formatting ever
- Customer may speak a different language - understand them, respond in YOUR language only

{f"Recent conversation:{chr(10)}{history_context}{chr(10)}" if history_context else ""}

User said: "{transcript}"

Reply naturally:'''

    return prompt


# =============================================================================
# MAIN PIPELINE - Enhanced conversation handling with language support
# =============================================================================
async def main_pipeline(websocket, audio_bytes: bytes, settings: dict, session_id: str = None):
    pipeline_start_time = time.time()
    metrics = {
        'asr_processing_time': 0,
        'total_processing_time': 0,
        'llm_tts_metrics': {
            'first_llm_token_time': None,
            'llm_token_latencies': [],
            'first_tts_chunk_time': None,
            'tts_chunk_latencies': [],
            'audio_generation_time': 0
        }
    }
    tts_tasks, input_audio_file = [], None
    db_manager = None
    conversation = None

    db_enabled = settings.get('db_enabled', False)
    reset_session = settings.get('reset_session', False)
    
    # Get response language from TTS settings (this is the Voice Output language)
    response_lang = settings.get('tts_language_code', 'en')
    # Normalize language code (en-US -> en), but keep zh-cn
    if response_lang.lower() != 'zh-cn' and '-' in response_lang:
        response_lang = response_lang.split('-')[0].lower()
    else:
        response_lang = response_lang.lower()
    logging.info(f"[Lang] Response language: {response_lang}")
    
    if db_enabled and ASYNCPG_AVAILABLE:
        # USE GLOBAL POOL - Fixes "no voice" latency/timeout issues
        db_host = settings.get('db_host', 'localhost')
        db_port = settings.get('db_port', '5432')
        db_name = settings.get('db_name', 'customer_service')
        db_user = settings.get('db_user', 'agent')
        db_pass = settings.get('db_password', '')
        
        # Get global pool or create one
        shared_pool = await get_db_pool(db_host, db_port, db_name, db_user, db_pass)
        
        # Initialize manager with shared pool
        db_manager = DatabaseManager(
            host=db_host, port=db_port, dbname=db_name, user=db_user, password=db_pass,
            pool=shared_pool
        )
        # connect() will detect the shared pool and just verify tables
        await db_manager.connect()
        
        if reset_session and session_id in conversation_sessions:
            conversation_sessions[session_id].reset()
            logging.info(f"[Session] Reset requested for: {session_id}")
        
        if session_id not in conversation_sessions:
            # Try to load from DB
            saved_data = await db_manager.load_session(session_id)
            if saved_data:
                conversation_sessions[session_id] = ConversationState.from_dict(saved_data)
                logging.info(f"[Session] Loaded session from DB: {session_id}")
            else:
                conversation_sessions[session_id] = ConversationState()
                logging.info(f"[Session] Created new session: {session_id}")
        
        conversation = conversation_sessions[session_id]
        logging.info(f"[Session] ID: {session_id}, State: {conversation.state}, Identified: {conversation.is_identified}")

    resampled_file = None
    input_audio_file = None

    try:
        # Use in-memory buffer for initial inspection to avoid disk I/O
        audio_buffer = io.BytesIO(audio_bytes)
        sample_rate, n_channels, inspect_status = inspect_wav_properties(audio_buffer)
        
        logging.info(inspect_status)
        if sample_rate == 0:
            await websocket.send(inspect_status)
            return

        # --- ASR Stage ---
        asr_start_time = time.time()
        
        asr_sample_rate = 16000
        
        # Optimize: Only write to disk if resampling is needed
        if sample_rate != asr_sample_rate:
            # Must write to file for ffmpeg resampling
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                input_audio_file = tmp.name
                tmp.write(audio_bytes)
                
            asr_audio_file, needs_cleanup = await resample_audio_async(input_audio_file, asr_sample_rate)
            if needs_cleanup:
                resampled_file = asr_audio_file
                
            # Transcribe from file (resampled)
            final_transcript = await transcribe_with_whisper(asr_audio_file, settings)
        else:
            # Direct in-memory transcription (Fast path)
            final_transcript = await transcribe_with_whisper(audio_bytes, settings)
            
        metrics['asr_processing_time'] = time.time() - asr_start_time

        # Filter noise
        noise_patterns = {'-', '--', '...', '.', '..', '—', '–', '', ' ', 'uh', 'um', 'hmm', 'hm', 'ah', 'eh'}
        cleaned_transcript = final_transcript.strip() if final_transcript else ''
        
        words = re.findall(r'\b\w+\b', cleaned_transcript.lower())
        word_count = len(words)
        
        valid_single_words = {'hi', 'hey', 'hello', 'bye', 'goodbye', 'yes', 'no', 'help', 'stop', 
                              'start', 'thanks', 'please', 'okay', 'ok', 'sure', 'what', 'why', 
                              'how', 'when', 'where', 'who', 'cancel', 'confirm', 'standard', 'premium'}
        
        single_word_noise = {'you', 'me', 'the', 'a', 'an', 'is', 'are', 'was', 'were', 'so', 
                             'and', 'but', 'or', 'if', 'then', 'well', 'like', 'just', 'right', 
                             'huh', 'uh', 'um', 'oh'}
        
        is_noise = (
            not cleaned_transcript or 
            cleaned_transcript.lower() in noise_patterns or 
            len(cleaned_transcript) < 2 or
            (word_count == 1 and words[0] in single_word_noise and words[0] not in valid_single_words)
        )
        
        if is_noise:
            logging.info(f"[ASR] Ignoring noise/short transcript: \"{final_transcript}\" (words: {word_count})")
            await websocket.send("Status: Listening... (speak a complete phrase)")
            return
            
        logging.info(f"[ASR] Transcript: \"{final_transcript}\" (words: {word_count})")
        
        # --- Sentiment Analysis (use ASR language for detection) ---
        asr_lang = settings.get('asr_language_code', 'en')
        sentiment = analyze_sentiment(final_transcript, asr_lang)
        if sentiment and sentiment.get('needs_alert') and db_manager:
            # Get customer info if identified
            cust_id = conversation.customer_id if conversation else None
            cust_name = conversation.customer_name if conversation else "Unknown"
            cust_email = conversation.customer_email if conversation else None
            
            # Create context summary from recent conversation
            context = ""
            if conversation and conversation.history:
                recent = conversation.history[-6:]  # Last 3 exchanges
                context = " | ".join([f"{m['role']}: {m['content'][:100]}" for m in recent])
            
            # Save sentiment alert to database
            await db_manager.create_sentiment_alert(
                customer_id=cust_id,
                customer_name=cust_name,
                customer_email=cust_email,
                sentiment_score=sentiment['score'],
                sentiment_label=sentiment['label'],
                trigger_phrases=", ".join(sentiment['trigger_phrases']),
                customer_message=final_transcript[:500],
                context_summary=context[:1000] if context else None,
                churn_risk=sentiment['churn_risk'],
                recommended_action=sentiment['recommended_action'],
                session_id=session_id
            )
            
            logging.warning(f"[SENTIMENT] Detected {sentiment['label']} sentiment (score: {sentiment['score']}, risk: {sentiment['churn_risk']})")
        
        # --- Build Response ---
        response_text = ""
        use_llm = False
        full_response = ""
        
        if db_manager and conversation:
            conversation.add_message("user", final_transcript)
            
            # =====================================================
            # STATE MACHINE FOR CONVERSATION FLOW
            # =====================================================
            
            # STATE: Upgrade - Selecting Plan
            if conversation.state == ConversationState.STATE_UPGRADE_SELECT_PLAN:
                plan_choice = extract_plan_choice(final_transcript)
                
                if plan_choice:
                    # User selected a plan
                    plan = await db_manager.get_plan_by_name(plan_choice)
                    if plan:
                        conversation.set_pending_upgrade(plan['name'], plan.get('id'))
                        response_text = get_natural_response('upgrade_confirm', lang=response_lang, plan=plan['name'])
                    else:
                        response_text = f"I couldn't find a plan matching '{plan_choice}'. We have Standard and Premium - which would you prefer?"
                elif is_cancellation(final_transcript):
                    conversation.cancel_upgrade()
                    response_text = get_natural_response('upgrade_cancelled', lang=response_lang)
                else:
                    response_text = "Just to clarify - are you looking at Standard or Premium?"
            
            # STATE: Upgrade - Awaiting Confirmation
            elif conversation.state == ConversationState.STATE_UPGRADE_CONFIRM:
                if is_confirmation(final_transcript):
                    # User confirmed - create the upgrade request
                    plan_name = conversation.pending_upgrade_plan
                    plan_id = conversation.pending_upgrade_plan_id
                    
                    # Get current plan details
                    current_plan_results, _ = await db_manager.run_predefined_query('current_plan', conversation.customer_id)
                    current_plan_id = current_plan_results[0]['id'] if current_plan_results else None
                    current_plan_name = current_plan_results[0]['name'] if current_plan_results else None
                    
                    # Create upgrade request in database with full details
                    result, error = await db_manager.create_upgrade_request(
                        customer_id=conversation.customer_id,
                        customer_name=conversation.customer_name,
                        customer_email=conversation.customer_email,
                        current_plan_id=current_plan_id,
                        current_plan_name=current_plan_name,
                        target_plan_id=plan_id,
                        target_plan_name=plan_name,
                        session_id=session_id
                    )
                    
                    if error:
                        logging.error(f"[Upgrade] Failed to create request: {error}")
                        response_text = "I ran into a small issue. Can you confirm the upgrade again?"
                    else:
                        completed_plan = conversation.complete_upgrade()
                        response_text = get_natural_response('upgrade_submitted', lang=response_lang, plan=completed_plan)
                        logging.info(f"[Upgrade] Request #{result} created: {conversation.customer_name} ({conversation.customer_email}) -> {completed_plan}")
                        
                elif is_cancellation(final_transcript):
                    conversation.cancel_upgrade()
                    response_text = get_natural_response('upgrade_cancelled', lang=response_lang)
                else:
                    # Unclear response - ask again
                    response_text = f"Sorry, I didn't catch that. You want to upgrade to {conversation.pending_upgrade_plan}, right? Yes or no."
            
            # STATE: Create Ticket - Awaiting Details
            elif conversation.state == ConversationState.STATE_CREATE_TICKET:
                logging.info(f"[Ticket] Processing description: {final_transcript}")
                
                if is_cancellation(final_transcript):
                    conversation.state = ConversationState.STATE_IDENTIFIED
                    response_text = "Okay, I won't open a ticket."
                    logging.info("[Ticket] Creation cancelled by user")
                else:
                    # Use transcript as subject (reason) as requested by user
                    # "The subject in the ticket needs to be the reason the customer says"
                    subject = final_transcript[:200] if final_transcript else "Voice Request"
                    description = f"Created via Voice Agent on {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\nDetails: {final_transcript}"
                    
                    logging.info(f"[Ticket] Creating ticket for customer {conversation.customer_id}...")
                    ticket_id, error = await db_manager.create_ticket(conversation.customer_id, subject, description)
                    
                    if ticket_id:
                        logging.info(f"[Ticket] Success! ID: {ticket_id}")
                        response_text = get_natural_response('ticket_created', lang=response_lang, id=ticket_id)
                        
                        # FORCE REFRESH of customer info so the new ticket shows up in UI immediately
                        try:
                            # Re-fetch customer info logic
                            tickets, _ = await db_manager.run_predefined_query('tickets', conversation.customer_id)
                            # ... (simplified re-fetch for UI update)
                            # We will rely on the UI auto-refresh polling, but we can also push an update if needed.
                            # For now, relying on the 'ticket_created' response is enough, 
                            # but let's log that we expect the UI to update.
                            logging.info("[Ticket] Ticket created, UI should update on next poll")
                        except Exception as e:
                            logging.error(f"[Ticket] Error during post-creation info fetch: {e}")
                            
                    else:
                        logging.error(f"[Ticket] Creation failed: {error}")
                        response_text = "I had a bit of trouble opening that ticket. You might want to try again later."
                    
                    # Reset state
                    conversation.state = ConversationState.STATE_IDENTIFIED

            # STATE: Close Ticket - Awaiting Confirmation (if ambiguous)
            # (Currently logic closes latest automatically, but this state is reserved for future specific selection)

            # STATE: Awaiting PIN
            elif conversation.state == ConversationState.STATE_AWAITING_PIN or conversation.awaiting_pin:
                # Use ASR language for PIN extraction (what the user speaks)
                asr_lang = settings.get('asr_language_code', 'en')
                pin = extract_pin_from_text(final_transcript, asr_lang)
                logging.info(f"[Auth] Extracted PIN: {pin} (lang: {asr_lang})")
                
                if pin:
                    customer = await db_manager.find_customer_by_pin(pin)
                    if customer:
                        conversation.identify_customer(
                            customer['id'], 
                            customer['name'], 
                            customer['email'],
                            pin
                        )
                        
                        # --- Send Customer Info to UI (Enhanced with full history) ---
                        try:
                            # Fetch summary data for UI popup
                            tickets, _ = await db_manager.run_predefined_query('tickets', customer['id'])
                            invoices, _ = await db_manager.run_predefined_query('invoices', customer['id'])
                            subscription, _ = await db_manager.run_predefined_query('subscription', customer['id'])
                            customer_details, _ = await db_manager.run_predefined_query('customer_info', customer['id'])
                            
                            open_tickets = len([t for t in tickets if t['status'] == 'open']) if tickets else 0
                            overdue_inv = len([i for i in invoices if i['status'] == 'overdue']) if invoices else 0
                            pending_inv = len([i for i in invoices if i['status'] == 'pending']) if invoices else 0
                            paid_inv = len([i for i in invoices if i['status'] == 'paid']) if invoices else 0
                            
                            # Get subscription info
                            plan_name = ''
                            plan_price = ''
                            subscription_status = ''
                            if subscription and len(subscription) > 0:
                                plan_name = subscription[0].get('plan_name', '')
                                plan_price = f"{subscription[0].get('price', 0):.2f}"
                                subscription_status = subscription[0].get('status', '')
                            
                            # Get customer since date
                            customer_since = ''
                            if customer_details and len(customer_details) > 0:
                                created_at = customer_details[0].get('created_at')
                                if created_at:
                                    customer_since = created_at.strftime('%b %Y') if hasattr(created_at, 'strftime') else str(created_at)[:7]
                            
                            # Get recent tickets with details
                            recent_tickets = []
                            if tickets:
                                for t in tickets[:3]:
                                    recent_tickets.append({
                                        'subject': t.get('subject', 'No subject'),
                                        'status': t.get('status', 'open'),
                                        'priority': t.get('priority', 'medium')
                                    })
                            
                            info_payload = {
                                "type": "customer_info",
                                "data": {
                                    "customer_id": customer['id'],  # For auto-refresh
                                    "name": customer['name'],
                                    "email": customer['email'],
                                    "phone": customer['phone'],
                                    "open_tickets": open_tickets,
                                    "overdue_invoices": overdue_inv,
                                    "pending_invoices": pending_inv,
                                    # Extended info
                                    "plan_name": plan_name,
                                    "plan_price": plan_price,
                                    "subscription_status": subscription_status,
                                    "total_tickets": len(tickets) if tickets else 0,
                                    "total_invoices": len(invoices) if invoices else 0,
                                    "paid_invoices": paid_inv,
                                    "customer_since": customer_since,
                                    "recent_tickets": recent_tickets
                                }
                            }
                            await websocket.send(json.dumps(info_payload))
                            logging.info(f"[UI] Sent customer info for {customer['name']}")
                        except Exception as e:
                            logging.error(f"[UI] Failed to send info: {e}")
                        # --------------------------------
                        
                        response_text = get_natural_response('greeting_returning', lang=response_lang, name=customer['name'].split()[0])
                    else:
                        conversation.pin_attempts += 1
                        if conversation.pin_attempts >= 3:
                            response_text = get_natural_response('pin_locked', lang=response_lang)
                            conversation.awaiting_pin = False
                            conversation.state = ConversationState.STATE_NEW
                        else:
                            remaining = 3 - conversation.pin_attempts
                            response_text = get_natural_response('pin_invalid', lang=response_lang, remaining=remaining)
                else:
                    response_text = get_natural_response('pin_unclear', lang=response_lang)
            
            # STATE: Not identified yet - ask for PIN
            elif not conversation.is_identified:
                conversation.awaiting_pin = True
                conversation.state = ConversationState.STATE_AWAITING_PIN
                response_text = get_natural_response('greeting_new', lang=response_lang)
            
            # STATE: Identified - Handle queries
            else:
                query_type = detect_query_type(final_transcript)
                
                # Special handling for upgrade requests
                if query_type == 'upgrade':
                    logging.info("[Query] Starting upgrade flow")
                    conversation.start_upgrade_flow()
                    response_text = get_natural_response('upgrade_ask_plan', lang=response_lang)
                
                elif query_type == 'create_ticket':
                    logging.info("[Query] Starting create ticket flow")
                    conversation.state = ConversationState.STATE_CREATE_TICKET
                    response_text = get_natural_response('ticket_ask_details', lang=response_lang)

                elif query_type == 'close_ticket':
                    logging.info("[Query] Processing close ticket request")
                    
                    # Check for specific ticket number in transcript
                    ticket_number = None
                    numbers = re.findall(r'\b\d+\b', final_transcript)
                    if numbers:
                        ticket_number = numbers[-1] # Assume the last number is the ID if present
                    
                    if ticket_number:
                        logging.info(f"[Query] Closing specific ticket ID: {ticket_number}")
                        result, error = await db_manager.close_ticket_by_id(conversation.customer_id, ticket_number)
                        if result:
                            t_id, subject = result
                            response_text = f"Done. I've closed ticket number {t_id} about '{subject}'."
                        else:
                            response_text = f"I couldn't find an open ticket with number {ticket_number}. Want me to list your open tickets?"
                    else:
                        # Logic: Fetch open tickets first
                        open_tickets, error = await db_manager.get_open_tickets(conversation.customer_id)
                        
                        if not open_tickets:
                            response_text = "I checked, but you don't have any open support tickets right now."
                        elif len(open_tickets) == 1:
                            # Single ticket - close it
                            t_id = open_tickets[0]['id']
                            subject = open_tickets[0]['subject']
                            result, error = await db_manager.close_ticket_by_id(conversation.customer_id, t_id)
                            if result:
                                response_text = f"I've closed your open ticket, number {t_id}, regarding '{subject}'."
                            else:
                                response_text = "I tried to close that ticket but ran into an issue."
                        else:
                            # Multiple tickets - close latest but inform user
                            latest = open_tickets[0]
                            t_id = latest['id']
                            subject = latest['subject']
                            count = len(open_tickets)
                            
                            result, error = await db_manager.close_ticket_by_id(conversation.customer_id, t_id)
                            if result:
                                response_text = f"You have {count} open tickets. I've closed the most recent one, ticket {t_id}, about '{subject}'."
                            else:
                                response_text = "I ran into an issue updating your ticket."

                elif query_type:
                    logging.info(f"[Query] Detected type: {query_type}")
                    results, error = await db_manager.run_predefined_query(query_type, conversation.customer_id)
                    
                    if error:
                        logging.error(f"[Query] Error: {error}")
                        response_text = get_natural_response('query_error', lang=response_lang)
                    elif results:
                        response_text = format_query_results(query_type, results, conversation.customer_name, response_lang)
                        
                        # --- SEND RICH CARD ---
                        try:
                            card_payload = generate_card_payload(query_type, results)
                            if card_payload:
                                await websocket.send(json.dumps(card_payload))
                                logging.info(f"[UI] Sent rich card: {query_type}")
                        except Exception as e:
                            logging.error(f"[UI] Failed to send card: {e}")
                    else:
                        response_text = get_natural_response('info_not_found', lang=response_lang, type=query_type)
                else:
                    # General conversation - use LLM
                    use_llm = True
        else:
            # Non-database mode - use LLM
            use_llm = True
        
        # --- LLM + TTS Stage ---
        llm_tts_start_time = time.time()

        if response_text and not use_llm:
            # Use predefined response
            logging.info(f"[Response] Using predefined: {response_text[:50]}...")
            
            task = asyncio.create_task(
                stream_tts_for_sentence(
                    websocket, response_text,
                    llm_tts_start_time, metrics, settings
                )
            )
            tts_tasks.append(task)
            await task
            
            full_response = response_text
        else:
            # Use LLM for response
            llm_api_base = settings.get('llm_api_base')
            llm_api_key = settings.get('llm_api_key')
            
            # Get or create conversation history
            if session_id not in conversation_sessions:
                conversation_sessions[session_id] = ConversationState()
            conv_state = conversation_sessions[session_id]
            
            if not (db_manager and conversation):
                conv_state.add_message("user", final_transcript)
            
            # Build history context
            history_context = ""
            recent_history = conv_state.history[-8:]
            if len(recent_history) > 2:
                history_context = ""
                for msg in recent_history[:-1]:
                    role = "Customer" if msg["role"] == "user" else "Agent"
                    history_context += f"{role}: {msg['content']}\n"
            
            # Build prompt with language instruction
            if conversation and conversation.is_identified:
                prompt = build_llm_prompt(final_transcript, conversation, history_context, response_lang, sentiment)
            else:
                custom_template = settings.get('llm_prompt_template')
                prompt = build_general_llm_prompt(final_transcript, history_context, custom_template, response_lang)
            
            logging.info(f"[LLM] Generating response...")
            
            # Validate URL
            if not llm_api_base:
                logging.error("[LLM] No API base URL configured")
                await websocket.send("Error: LLM API base URL not configured")
                return
            
            if llm_api_base.startswith('ttps://'):
                llm_api_base = 'h' + llm_api_base
            elif not llm_api_base.startswith('http'):
                llm_api_base = 'https://' + llm_api_base
            
            llm_api_base = llm_api_base.rstrip('/')
            llm_url = f"{llm_api_base}/chat/completions"
            logging.info(f"[LLM] URL: {llm_url[:60]}...")
            
            llm_headers = {
                "Authorization": f"Bearer {llm_api_key}",
                "Content-Type": "application/json"
            }
            llm_payload = {
                "model": settings.get('llm_model_name'),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 150,
                "stream": True
            }
            
            sentence_buffer = ""
            full_response = ""
            
            async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
                async with client.stream("POST", llm_url, headers=llm_headers, json=llm_payload) as response:
                    response.raise_for_status()
                    
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        
                        try:
                            chunk_data = json.loads(data_str)
                            if metrics['llm_tts_metrics']['first_llm_token_time'] is None:
                                metrics['llm_tts_metrics']['first_llm_token_time'] = time.time() - llm_tts_start_time
                            
                            choices = chunk_data.get('choices', [])
                            if choices:
                                delta = choices[0].get('delta', {})
                                token = delta.get('content', '')
                                if token:
                                    sentence_buffer += token
                                    full_response += token
                                    
                                    while True:
                                        split_pos = -1
                                        for delim in SENTENCE_TERMINATORS:
                                            pos = sentence_buffer.find(delim)
                                            if pos != -1 and (split_pos == -1 or pos < split_pos):
                                                split_pos = pos
                                        
                                        if split_pos != -1:
                                            dispatch_text = sentence_buffer[:split_pos + 1]
                                            
                                            # Buffer very short sentences to avoid voice switching artifacts
                                            # If text is too short (e.g. "Ok."), keep it in buffer unless it's a clear end
                                            if len(dispatch_text) < 15 and len(sentence_buffer) > split_pos + 1:
                                                # Look ahead - if we have more text, maybe wait
                                                # But simpler: just process it if it's a sentence
                                                pass
                                            
                                            sentence_buffer = sentence_buffer[split_pos + 1:]
                                            task = asyncio.create_task(
                                                stream_tts_for_sentence(
                                                    websocket, dispatch_text,
                                                    llm_tts_start_time, metrics, settings
                                                )
                                            )
                                            tts_tasks.append(task)
                                            await task
                                        else:
                                            break
                        except json.JSONDecodeError:
                            continue
            
            # Handle remaining text
            if sentence_buffer.strip():
                task = asyncio.create_task(
                    stream_tts_for_sentence(
                        websocket, sentence_buffer,
                        llm_tts_start_time, metrics, settings
                    )
                )
                tts_tasks.append(task)
                await task

        # Safe gather for TTS tasks
        if tts_tasks:
            await asyncio.gather(*tts_tasks, return_exceptions=True)
        
        # Save response to history
        if conversation:
            conversation.add_message("assistant", full_response)
        elif session_id in conversation_sessions:
            conversation_sessions[session_id].add_message("assistant", full_response)
        
        # Save session state to DB
        if db_manager and conversation:
            await db_manager.save_session(
                session_id, 
                conversation.customer_id, 
                conversation.to_dict()
            )

        # Latency report
        metrics['total_processing_time'] = time.time() - pipeline_start_time
        m = metrics['llm_tts_metrics']
        
        logging.info("--- Latency Report ---")
        logging.info(f"ASR Time: {metrics['asr_processing_time']:.4f}s")
        if m['first_llm_token_time']:
            logging.info(f"First LLM Token: {m['first_llm_token_time']:.4f}s")
        if m['first_tts_chunk_time']:
            logging.info(f"First TTS Chunk: {m['first_tts_chunk_time']:.4f}s")
        logging.info(f"Total Time: {metrics['total_processing_time']:.4f}s")
        
        logging.info(f"Response: \"{full_response[:100]}...\"")
        if conversation:
            logging.info(f"Customer: {conversation.customer_name or 'Not identified'}")
            logging.info(f"State: {conversation.state}")
        history_len = len(conversation_sessions.get(session_id, ConversationState()).history) if session_id else 0
        logging.info(f"History length: {history_len}")
        logging.info("--- End of Report ---")
        
        # Send metrics to client
        try:
            metrics_payload = {
                "type": "latency_report",
                "data": {
                    "asr_processing_time": metrics['asr_processing_time'],
                    "first_llm_token_time": m['first_llm_token_time'],
                    "first_tts_chunk_time": m['first_tts_chunk_time'],
                    "total_processing_time": metrics['total_processing_time']
                }
            }
            await websocket.send(json.dumps(metrics_payload))
        except Exception as e:
            logging.error(f"Failed to send metrics: {e}")

        await websocket.send("__END_OF_STREAM__")

    except Exception as e:
        err = f"Pipeline error: {e}\n{traceback.format_exc()}"
        logging.error(err)
        try:
            await websocket.send(f"Server Error: {e}")
        except websockets.ConnectionClosed:
            pass
    finally:
        for task in tts_tasks:
            task.cancel()
        
        for temp_file in [input_audio_file, resampled_file]:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError as e:
                    logging.warning(f"Failed to remove temp file {temp_file}: {e}")
        
        if db_manager:
            await db_manager.disconnect()


# =============================================================================
# SESSION MANAGEMENT
# =============================================================================
def cleanup_old_sessions(max_age_minutes: int = 30):
    """Remove sessions older than max_age_minutes"""
    now = datetime.now()
    expired = [sid for sid, state in conversation_sessions.items() 
               if now - state.last_activity > timedelta(minutes=max_age_minutes)]
    for sid in expired:
        del conversation_sessions[sid]
    if expired:
        logging.info(f"Cleaned up {len(expired)} expired session(s)")


async def handler(websocket):
    """Handles incoming WebSocket connections."""
    client_id = str(websocket.remote_address)
    
    if not _rate_limiter.is_allowed(client_id):
        logging.warning(f"Rate limit exceeded for {client_id}")
        try:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "Rate limit exceeded. Please wait."
            }))
            return
        except:
            pass
            
    logging.info(f"Client connected: {client_id}")
    current_settings = DEFAULT_SETTINGS.copy()
    audio_bytes = None
    session_id = None
    
    # cleanup handled by periodic task now

    try:
        first_message = await websocket.recv()

        try:
            client_settings = json.loads(first_message)
            current_settings.update(client_settings)
            session_id = client_settings.get('session_id', str(websocket.remote_address))
            logging.info(f"Session ID: {session_id}")
            
            # Check if this is a refresh_customer_info request
            if client_settings.get('action') == 'refresh_customer_info':
                # Get conversation session
                conversation = session_manager.get(session_id)
                if conversation and conversation.is_identified and conversation.customer_id:
                    try:
                        # Use the global db pool if available
                        db_pool = await ensure_db_pool()
                        if db_pool:
                            async with db_pool.acquire() as conn:
                                # Fetch tickets
                                tickets_rows = await conn.fetch("""
                                    SELECT id, subject, status, priority, created_at
                                    FROM support_tickets WHERE customer_id = $1
                                    ORDER BY created_at DESC LIMIT 10
                                """, conversation.customer_id)
                                tickets = [dict(r) for r in tickets_rows]
                                
                                # Fetch invoices
                                invoices_rows = await conn.fetch("""
                                    SELECT i.id, i.amount, i.status, i.due_date
                                    FROM invoices i WHERE i.customer_id = $1
                                    ORDER BY i.due_date DESC LIMIT 10
                                """, conversation.customer_id)
                                invoices = [dict(r) for r in invoices_rows]
                                
                                # Fetch subscription
                                subscription_row = await conn.fetchrow("""
                                    SELECT p.name as plan_name, p.price, s.status
                                    FROM subscriptions s
                                    JOIN plans p ON s.plan_id = p.id
                                    WHERE s.customer_id = $1
                                    ORDER BY s.start_date DESC LIMIT 1
                                """, conversation.customer_id)
                                subscription = dict(subscription_row) if subscription_row else None
                                
                                # Fetch customer details
                                customer_details_row = await conn.fetchrow("""
                                    SELECT phone, created_at FROM customers WHERE id = $1
                                """, conversation.customer_id)
                                customer_details = dict(customer_details_row) if customer_details_row else None
                                
                                # Calculate stats
                                open_tickets = len([t for t in tickets if t['status'] == 'open']) if tickets else 0
                                overdue_inv = len([i for i in invoices if i['status'] == 'overdue']) if invoices else 0
                                paid_inv = len([i for i in invoices if i['status'] == 'paid']) if invoices else 0
                                
                                # Get subscription info
                                plan_name = subscription['plan_name'] if subscription else ''
                                plan_price = f"{subscription['price']:.2f}" if subscription else ''
                                subscription_status = subscription['status'] if subscription else ''
                                
                                # Get customer since date
                                customer_since = ''
                                if customer_details and customer_details.get('created_at'):
                                    created_at = customer_details['created_at']
                                    customer_since = created_at.strftime('%b %Y') if hasattr(created_at, 'strftime') else str(created_at)[:7]
                                
                                # Get recent tickets
                                recent_tickets = []
                                if tickets:
                                    for t in tickets[:3]:
                                        recent_tickets.append({
                                            'subject': t.get('subject', 'No subject'),
                                            'status': t.get('status', 'open'),
                                            'priority': t.get('priority', 'medium')
                                        })
                                
                                info_payload = {
                                    "type": "customer_info",
                                    "data": {
                                        "name": conversation.customer_name,
                                        "email": conversation.customer_email,
                                        "phone": customer_details.get('phone', 'N/A') if customer_details else 'N/A',
                                        "open_tickets": open_tickets,
                                        "overdue_invoices": overdue_inv,
                                        "plan_name": plan_name,
                                        "plan_price": plan_price,
                                        "subscription_status": subscription_status,
                                        "total_tickets": len(tickets) if tickets else 0,
                                        "total_invoices": len(invoices) if invoices else 0,
                                        "paid_invoices": paid_inv,
                                        "customer_since": customer_since,
                                        "recent_tickets": recent_tickets
                                    }
                                }
                                await websocket.send(json.dumps(info_payload))
                                logging.info(f"[UI] Refreshed customer info for {conversation.customer_name}")
                        else:
                            logging.warning("[UI] No database pool available for refresh")
                    except Exception as e:
                        logging.error(f"[UI] Failed to refresh customer info: {e}")
                return  # Don't process audio for refresh requests
            
            audio_bytes = await websocket.recv()

        except (json.JSONDecodeError, TypeError):
            session_id = str(websocket.remote_address)
            if isinstance(first_message, bytes):
                audio_bytes = first_message
            else:
                raise ValueError("Received non-JSON, non-bytes message.")

        logging.info(f"Received {len(audio_bytes)} bytes of audio data.")
        await main_pipeline(websocket, audio_bytes, current_settings, session_id)

    except websockets.ConnectionClosed:
        logging.info(f"Client disconnected: {websocket.remote_address}")
    except Exception as e:
        logging.error(f"Error with client {websocket.remote_address}: {e}\n{traceback.format_exc()}")


# =============================================================================
# HEALTH CHECK & SERVER
# =============================================================================
async def health_handler(reader, writer):
    await reader.readline()
    
    db_status = "connected" if _db_pool else "not_configured"
    http_status = "connected" if _http_client and not _http_client.is_closed else "initializing"
    sessions_count = len(conversation_sessions)
    
    health_data = {
        "status": "healthy",
        "version": "4.1.0",
        "database_pool": db_status,
        "http_client": http_status,
        "active_sessions": sessions_count
    }
    
    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{json.dumps(health_data)}"
    writer.write(response.encode())
    await writer.drain()
    writer.close()


async def start_health_server():
    server = await asyncio.start_server(health_handler, "0.0.0.0", 8766)
    logging.info("Health check server: http://0.0.0.0:8766/health")
    async with server:
        await server.serve_forever()


async def periodic_session_cleanup(interval_seconds: int = 300):
    """Periodically cleanup expired sessions"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            cleanup_old_sessions()
        except Exception as e:
            logging.error(f"Session cleanup error: {e}")


async def graceful_shutdown():
    """Cleanup on shutdown"""
    logging.info("Shutting down gracefully...")
    await close_http_client()
    await close_db_pool()
    logging.info("Cleanup complete")


async def main():
    host, port = "0.0.0.0", 8765
    logging.info("=" * 60)
    logging.info("Voice Agent WebSocket Server v4.1.0")
    logging.info("Natural Conversation + Smart Upgrade Flow")
    logging.info("Whisper ASR + XTTS v2 TTS")
    logging.info("=" * 60)
    logging.info(f"WebSocket: ws://{host}:{port}")
    logging.info(f"ASR Server: {DEFAULT_SETTINGS['asr_server_address']}")
    logging.info(f"TTS Server: {DEFAULT_SETTINGS['tts_server_address']}")
    logging.info(f"Database enabled: {DEFAULT_SETTINGS['db_enabled']}")
    logging.info("=" * 60)
    
    asyncio.create_task(start_health_server())
    asyncio.create_task(periodic_session_cleanup(300))
    
    await get_http_client()
    logging.info("HTTP client pool initialized")
    
    try:
        async with websockets.serve(handler, host, port, max_size=50 * 1024 * 1024):
            await asyncio.Future()
    finally:
        await graceful_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server shutdown.")
    except Exception as e:
        logging.error(f"Server error: {e}")