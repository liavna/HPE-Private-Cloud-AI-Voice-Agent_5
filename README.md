# Voice Agent

🎙️ Voice Agent - Whisper ASR + XTTS v2 TTS + LLM

## Overview

Complete voice agent system with:
- **WebSocket Server** - Real-time audio processing pipeline
- **Gradio UI** - Modern web interface with conversation history
- **XTTS v2 TTS** - High-quality multilingual text-to-speech with GPU

## Features

- ✅ Push-to-Talk and Conversation Mode
- ✅ 17 supported languages (XTTS v2)
- ✅ Voice cloning capability
- ✅ All settings configurable via UI
- ✅ Conversation history
- ✅ Optional database integration
- ✅ GPU optimized TTS

## Directory Structure

```
voice-agent/
├── Chart.yaml              # Helm chart metadata
├── values.yaml             # Default values
├── build.sh                # Docker build script
├── templates/              # Kubernetes templates
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── virtualservice.yaml
│   └── ...
└── docker/                 # Docker source files
    ├── websocket-server/
    │   ├── Dockerfile
    │   ├── app.py
    │   └── requirements.txt
    ├── gradio-ui/
    │   ├── Dockerfile
    │   ├── app_ui.py
    │   └── requirements.txt
    └── xtts-server/
        ├── Dockerfile
        ├── xtts_server.py
        └── requirements.txt
```

## Prerequisites

- Docker with buildx
- Kubernetes 1.19+
- Helm 3.0+
- NVIDIA GPU (for XTTS v2)

## Building Docker Images

```bash
# Make build script executable
chmod +x build.sh

# Run interactive build menu
./build.sh

# Or build with custom registry
REGISTRY=myregistry ./build.sh

# Or build with custom version
VERSION=4.1.0 ./build.sh
```

### Manual Build

```bash
# WebSocket Server
cd docker/websocket-server
docker build -t liavna/web-socket-server:4.1.0 .

# Gradio UI
cd docker/gradio-ui
docker build -t liavna/web-socket-server-ui:4.1.0 .

# XTTS Server (GPU)
cd docker/xtts-server
docker build -t liavna/xtts-server:4.1.0 .
```

## Installation

```bash
# From directory
helm install voice-agent .

# From tar.gz
helm install voice-agent voice-agent-4.1.0.tar.gz

# With custom values
helm install voice-agent . -f my-values.yaml

# In specific namespace
helm install voice-agent . -n my-namespace
```

## Configuration

All ASR, LLM, and TTS settings are configured via the UI.

### Key Values

| Parameter | Description | Default |
|-----------|-------------|---------|
| `replicaCount` | Number of replicas | `1` |
| `websocketServer.image.tag` | WebSocket image tag | `4.1.0` |
| `gradioUi.image.tag` | Gradio UI image tag | `4.1.0` |
| `xttsTts.enabled` | Enable XTTS v2 | `true` |
| `xttsTts.image.tag` | XTTS image tag | `4.1.0` |
| `istioSidecar.enabled` | Enable Istio sidecar | `false` |
| `ezua.enabled` | Enable VirtualService | `true` |

### Disable Istio Sidecar

```yaml
istioSidecar:
  enabled: false
```

### GPU Resources

```yaml
xttsTts:
  resources:
    requests:
      nvidia.com/gpu: 1
    limits:
      nvidia.com/gpu: 1
```

## Supported Languages

XTTS v2 supports 17 languages:

| Language | Code | Language | Code |
|----------|------|----------|------|
| English | en | Polish | pl |
| Spanish | es | Turkish | tr |
| French | fr | Russian | ru |
| German | de | Dutch | nl |
| Italian | it | Czech | cs |
| Portuguese | pt | Arabic | ar |
| Chinese | zh-cn | Japanese | ja |
| Hungarian | hu | Korean | ko |
| Hindi | hi | | |

## Upgrading

```bash
helm upgrade voice-agent .
```

## Uninstalling

```bash
helm uninstall voice-agent
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                         Pod                              │
├─────────────────┬─────────────────┬─────────────────────┤
│  websocket-     │    gradio-ui    │     xtts-tts        │
│  server         │                 │     (GPU)           │
│  :8765          │     :8080       │     :8000           │
└─────────────────┴─────────────────┴─────────────────────┘
```

## License

MIT
