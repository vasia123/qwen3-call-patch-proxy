#!/usr/bin/env python3
"""
Setup script for qwen3-call-patch-proxy
"""

from setuptools import setup, find_packages
import os

# Read the README file
with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

# Read requirements
with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="qwen3-call-patch-proxy",
    version="1.0.0",
    author="Community",
    description="HTTP proxy that fixes malformed tool calls from Qwen3-Coder LLM models for OpenCode",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/qwen3-call-patch-proxy",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Internet :: Proxy Servers",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "qwen3-call-patch-proxy=call_patch_proxy:main",
        ],
    },
    include_package_data=True,
    package_data={
        "": ["*.yaml", "*.yml"],
    },
    keywords="qwen3 llm proxy tool-calls claude-code streaming sse",
    project_urls={
        "Bug Reports": "https://github.com/yourusername/qwen3-call-patch-proxy/issues",
        "Source": "https://github.com/yourusername/qwen3-call-patch-proxy",
        "Documentation": "https://github.com/yourusername/qwen3-call-patch-proxy#readme",
    },
)