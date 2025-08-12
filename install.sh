#!/bin/bash
#
# Qwen3 Call Patch Proxy Installation Script
#

set -e

echo "🔧 Installing Qwen3 Call Patch Proxy..."

# Check Python version
python_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
required_version="3.8"

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)"; then
    echo "❌ Error: Python 3.8+ required, found Python $python_version"
    exit 1
fi

echo "✅ Python $python_version detected"

# Install dependencies
echo "📦 Installing dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# Make scripts executable
chmod +x call_patch_proxy.py

# Test configuration
echo "🧪 Testing configuration..."
python3 -c "
import yaml
try:
    with open('tool_fixes.yaml', 'r') as f:
        config = yaml.safe_load(f)
    print('✅ Configuration file is valid')
except Exception as e:
    print(f'❌ Configuration error: {e}')
    exit(1)
"

echo "🎉 Installation complete!"
echo ""
echo "Usage:"
echo "  python3 call_patch_proxy.py          # Start the proxy"
echo "  python3 test_proxy.py                # Run tests"
echo ""
echo "The proxy will listen on: http://localhost:7999"
echo "Point Claude Code to this address instead of your Qwen3 server."
echo ""
echo "For help: https://github.com/yourusername/qwen3-call-patch-proxy"