# Qwen3 Call Patch Proxy

A robust HTTP proxy server that fixes malformed tool calls from Qwen3-Coder LLM models before sending them to OpenCode or other downstream services.

> **Note:** This proxy is primarily developed for [OpenCode](https://github.com/1rgs/opencode), but may be useful for other LLM environments requiring tool call format fixes.

## What It Does

Fixes Qwen3-Coder tool call issues:
- Consolidates fragmented tool calls across multiple SSE events
- Converts string parameters to proper types (arrays, booleans)
- Adds missing required parameters with sensible defaults
- Generates proper tool call IDs in OpenCode format

## Demo

<img src="images/OpenCode-Qwen3-Coder-GameOfLife.png" alt="OpenCode creating Game of Life with Qwen3-Coder" width="600">

*OpenCode successfully creating Conway's Game of Life using Qwen3-Coder through the proxy*

## Tested Models

- **unsloth/Qwen3-Coder-30B-A3B-Instruct**
- **cpatonn/Qwen3-Coder-30B-A3B-Instruct-AWQ**

## Quick Start

### Prerequisites
- Python 3.8+
- A running Qwen3-Coder model server (typically on port 8080)
- OpenCode or compatible client

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/qwen3-call-patch-proxy.git
   cd qwen3-call-patch-proxy
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start the proxy:**
   ```bash
   python call_patch_proxy.py
   ```

4. **Configure OpenCode to use the proxy:**
   Point OpenCode to `http://localhost:7999` instead of your model server directly.

### Architecture

```
OpenCode ──→ Proxy (7999) ──→ Qwen3 Server (8080)
             [Fixes tool calls]
```

## OpenCode Configuration

To use this proxy with OpenCode, add the following provider configuration to your OpenCode settings:

```json
{
  "provider": {
    "qwen3-coder-30b-a3b-instruct-vllm": {
      "name": "Qwen 3 Coder 30B (vLLM)",
      "npm": "@ai-sdk/openai-compatible",
      "models": {
        "cpatonn/Qwen3-Coder-30B-A3B-Instruct-AWQ": {}
      },
      "options": {
        "baseURL": "http://127.0.0.1:7999/v1"
      }
    }
  }
}
```

This configuration:
- Uses the proxy on port 7999 as the `baseURL`
- Works with OpenAI-compatible API format
- Supports AWQ quantized models for efficient inference

## Health Monitoring

- **Health check:** `GET http://localhost:7999/_health`
- **Reload config:** `POST http://localhost:7999/_reload`

## Documentation

For detailed information, see:
- [Detailed Guide](docs/detailed-guide.md) - Complete documentation with examples and configuration options

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make your changes and add tests
4. Commit: `git commit -m 'Add amazing feature'`
5. Push: `git push origin feature/amazing-feature`
6. Open a Pull Request

## License

This project is licensed under the GPL-3.0 License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built for the OpenCode ecosystem
- Designed to work with Qwen3-Coder models from Alibaba Cloud
- Inspired by the need for robust LLM tool call handling

---

**Need help?** Open an issue or check the [discussions](../../discussions) section!