#!/usr/bin/env python3
"""
Qwen3 Call Patch Proxy

A robust HTTP proxy server that fixes malformed tool calls from Qwen3-Coder LLM models
before sending them to OpenCode or other downstream services.

This proxy handles common issues like:
- String parameters that should be arrays/objects
- Fragmented tool calls across multiple SSE events
- Missing required parameters
- Incorrect parameter naming conventions
- Invalid tool call ID formats

Author: Community
License: MIT
Repository: https://github.com/yourusername/qwen3-call-patch-proxy
"""

import aiohttp
from aiohttp import web
import json
import logging
import re
import yaml
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
import uuid

# === CONFIGURATION ===
# Target server running your Qwen3-Coder model
TARGET_HOST = "http://127.0.0.1:8080"   
# Port where this proxy will listen for incoming requests
LISTEN_PORT = 7999                      
# Logging level: DEBUG for full trace, INFO for production
LOG_LEVEL = logging.DEBUG               
# YAML configuration file containing tool fix rules
CONFIG_FILE = "tool_fixes.yaml"

# Create logs directory if it doesn't exist
import os
os.makedirs("logs", exist_ok=True)

# Console logger - for tool calls and proxy changes only
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console_handler.setFormatter(console_formatter)

# File logger - for all detailed communication
file_handler = logging.FileHandler("logs/proxy_detailed.log")
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] [%(funcName)s:%(lineno)d] %(message)s")
file_handler.setFormatter(file_formatter)

# Main logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Console-only logger for important events
console_logger = logging.getLogger("console")
console_logger.setLevel(logging.INFO)
console_logger.addHandler(console_handler)
console_logger.propagate = False

@dataclass
class ToolBuffer:
    """Enhanced buffer for tracking tool call state"""
    call_id: str
    content: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    last_updated: datetime = field(default_factory=datetime.now)
    request_id: str = ""
    tool_name: str = ""
    
    def is_expired(self, timeout_seconds: int) -> bool:
        # Use last_updated instead of created_at for more accurate timeout
        return datetime.now() - self.last_updated > timedelta(seconds=timeout_seconds)
    
    def update_content(self, new_content: str):
        """Update content and refresh last_updated timestamp"""
        self.content += new_content
        self.last_updated = datetime.now()
    
    def size(self) -> int:
        return len(self.content.encode('utf-8'))

@dataclass 
class RequestState:
    """Per-request state management"""
    request_id: str
    tool_buffers: Dict[str, ToolBuffer] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    content_buffer: str = ""  # Buffer for accumulating XML content
    
    def cleanup_expired_buffers(self, timeout_seconds: int):
        expired_ids = [
            call_id for call_id, buffer in self.tool_buffers.items() 
            if buffer.is_expired(timeout_seconds)
        ]
        for call_id in expired_ids:
            logger.warning(f"[{self.request_id}] Cleaning up expired buffer: {call_id}")
            del self.tool_buffers[call_id]

class ToolFixEngine:
    """
    Configurable tool fix engine that applies transformations to tool call arguments.
    
    Loads fix rules from a YAML configuration file and applies them to incoming tool calls
    based on parameter conditions and desired actions.
    
    Supported fix actions:
    - parse_json_array: Convert JSON string to array
    - parse_json_object: Convert JSON string to object  
    - convert_string_to_boolean: Convert string to boolean
    - set_default: Set default value if missing
    
    Supported fix conditions:
    - is_string: Parameter is a string
    - missing: Parameter is missing
    - missing_or_empty: Parameter is missing or empty
    - invalid_enum: Parameter not in valid values list
    """
    
    def __init__(self, config_file: str):
        """Initialize the fix engine with configuration from YAML file."""
        self.config = self._load_config(config_file)
        self.settings = self.config.get('settings', {})
        logger.info(f"Loaded tool fix configuration with {len(self.config.get('tools', {}))} tools")
    
    def _load_config(self, config_file: str) -> Dict[str, Any]:
        try:
            with open(config_file, 'r') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            return self._get_default_config()
        except Exception as e:
            logger.error(f"Failed to load config: {e}, using defaults")
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Fallback configuration"""
        return {
            'tools': {
                'todowrite': {
                    'fixes': [{
                        'name': 'todos_string_to_array',
                        'parameter': 'todos',
                        'condition': 'is_string',
                        'action': 'parse_json_array',
                        'fallback_value': []
                    }]
                },
                'bash': {
                    'fixes': [{
                        'name': 'missing_description',
                        'parameter': 'description',
                        'condition': 'missing_or_empty', 
                        'action': 'set_default',
                        'default_value': 'Execute the given shell command'
                    }]
                },
                'read': {
                    'fixes': [{
                        'name': 'remove_content_parameter',
                        'parameter': 'content',
                        'condition': 'exists',
                        'action': 'remove_parameter'
                    }]
                }
            },
            'settings': {
                'buffer_timeout': 30,
                'max_buffer_size': 1048576,
                'detailed_logging': True,
                'case_sensitive_tools': False
            }
        }
    
    def get_setting(self, key: str, default=None):
        return self.settings.get(key, default)
    
    def apply_fixes(self, tool_name: str, args_obj: Dict[str, Any], request_id: str) -> tuple[str, Dict[str, Any]]:
        """Apply configured fixes to tool arguments. Returns (possibly_changed_tool_name, fixed_args)"""
        if not self.settings.get('case_sensitive_tools', False):
            tool_name = tool_name.lower()
        
        tool_config = self.config.get('tools', {}).get(tool_name, {})
        fixes = tool_config.get('fixes', [])
        
        if not fixes:
            logger.debug(f"[{request_id}] No fixes configured for tool: {tool_name}")
            return tool_name, args_obj
        
        result = args_obj.copy()
        applied_fixes = []
        final_tool_name = tool_name
        
        for fix in fixes:
            result_or_tuple = self._apply_single_fix(result, fix, request_id)
            if isinstance(result_or_tuple, tuple):
                # Tool conversion happened
                final_tool_name, applied = result_or_tuple
                if applied:
                    applied_fixes.append(fix['name'])
            elif result_or_tuple:
                applied_fixes.append(fix['name'])
        
        if applied_fixes:
            if final_tool_name != tool_name:
                console_logger.info(f"[{request_id}] 🔄 Converted {tool_name}→{final_tool_name}: {', '.join(applied_fixes)}")
            else:
                console_logger.info(f"[{request_id}] 🔧 Fixed {tool_name}: {', '.join(applied_fixes)}")
            logger.info(f"[{request_id}] Applied fixes to {tool_name}: {applied_fixes}")
        
        return final_tool_name, result
    
    def _apply_single_fix(self, args_obj: Dict[str, Any], fix: Dict[str, Any], request_id: str):
        """Apply a single fix rule. Returns bool or (new_tool_name, bool) for tool conversions"""
        param = fix['parameter']
        condition = fix['condition']
        action = fix['action']
        
        # Check condition
        if not self._check_condition(args_obj, param, condition, fix):
            return False
        
        # Apply action
        try:
            if action == 'parse_json_array':
                if isinstance(args_obj.get(param), str):
                    try:
                        args_obj[param] = json.loads(args_obj[param])
                    except json.JSONDecodeError:
                        # Try to fix common JSON issues like single quotes
                        fixed_json = self._fix_malformed_json(args_obj[param])
                        args_obj[param] = json.loads(fixed_json)
                        console_logger.info(f"[{request_id}] 🔧 Fixed malformed JSON for {param}")
                        logger.debug(f"[{request_id}] Fixed malformed JSON for {param}: {str(args_obj[param])[:100]}...")
            elif action == 'set_default':
                args_obj[param] = fix['default_value']
            elif action == 'parse_json_object':
                if isinstance(args_obj.get(param), str):
                    args_obj[param] = json.loads(args_obj[param])
            elif action == 'convert_string_to_boolean':
                if isinstance(args_obj.get(param), str):
                    value = args_obj[param].lower().strip()
                    args_obj[param] = value in ('true', '1', 'yes', 'on')
            elif action == 'remove_parameter':
                if param in args_obj:
                    del args_obj[param]
            elif action == 'convert_tool_to_write':
                # Convert read+content to write tool call
                if 'filePath' in args_obj and 'content' in args_obj:
                    # Keep both filePath and content for write tool
                    return ('write', True)
                else:
                    logger.warning(f"[{request_id}] Cannot convert to write: missing filePath or content")
                    return False
            return True
        except Exception as e:
            logger.warning(f"[{request_id}] Fix {fix['name']} failed: {e}")
            # Use fallback if available
            if 'fallback_value' in fix:
                args_obj[param] = fix['fallback_value']
                return True
        return False
    
    def _check_condition(self, args_obj: Dict[str, Any], param: str, condition: str, fix: Dict[str, Any]) -> bool:
        """Check if condition is met for applying fix"""
        value = args_obj.get(param)
        
        if condition == 'is_string':
            return isinstance(value, str)
        elif condition == 'missing_or_empty':
            return not value
        elif condition == 'missing':
            return param not in args_obj
        elif condition == 'exists':
            return param in args_obj
        elif condition == 'invalid_enum':
            valid_values = fix.get('valid_values', [])
            return value not in valid_values
        
        return False
    
    def _fix_malformed_json(self, json_str: str) -> str:
        """Fix common JSON formatting issues from LLMs"""
        if not json_str:
            return json_str
        
        # Fix single quotes to double quotes, but be careful about quotes inside strings
        fixed = json_str
        
        # Simple approach: replace single quotes with double quotes
        # This is not perfect but handles most LLM cases
        fixed = fixed.replace("'", '"')
        
        # Fix cases where we accidentally replaced quotes inside strings
        # This is a heuristic fix - not bulletproof but covers common cases
        import re
        
        # Try to fix over-quoted strings like "\"content\"" -> "content"
        fixed = re.sub(r'""([^"]*?)""', r'"\1"', fixed)
        
        return fixed

# Global instances
fix_engine = ToolFixEngine(CONFIG_FILE)
request_states: Dict[str, RequestState] = {}
# Track if we've detected legacy API mode automatically
legacy_mode_detected = False

def extract_model_from_request(data: bytes) -> Optional[str]:
    """
    Extract model name from request body.

    Returns model name if found, None otherwise.
    """
    if not data:
        return None

    try:
        body = json.loads(data)
        return body.get('model')
    except (json.JSONDecodeError, AttributeError):
        return None

def should_use_legacy_mode_for_model(model_name: Optional[str]) -> bool:
    """
    Check if a model should use legacy API mode.

    Returns True if:
    - Model is in legacy_models list
    - Legacy mode is globally enabled
    - Legacy mode was auto-detected
    """
    global legacy_mode_detected

    # Check if legacy mode is forced globally
    if fix_engine.get_setting('legacy_api_mode', False):
        return True

    # Check if legacy mode was auto-detected
    if legacy_mode_detected:
        return True

    # Check if model is in legacy models list
    if model_name:
        legacy_models = fix_engine.get_setting('legacy_models', [])
        if model_name in legacy_models:
            return True

    return False

async def read_legacy_stream(resp, request_id: str):
    """
    Robust streaming reader for legacy APIs with incorrect HTTP headers.

    This function reads the response using iter_any() which bypasses aiohttp's
    Transfer-Encoding parser, avoiding issues with incorrect headers.

    Yields raw_line bytes for each complete line found in the stream.
    """
    chunk_size = fix_engine.get_setting('legacy_read_chunk_size', 8192)
    read_timeout = fix_engine.get_setting('legacy_read_timeout', 30)

    buffer = b""  # Buffer for incomplete lines

    logger.info(f"[{request_id}] Using legacy stream reading mode (bypassing Transfer-Encoding parser)")

    try:
        # Use iter_any() instead of read() to bypass Transfer-Encoding checks
        async for chunk in resp.content.iter_any():
            try:
                if not chunk:
                    continue

                # Add chunk to buffer
                buffer += chunk

                # Split buffer by newlines and process complete lines
                lines = buffer.split(b'\n')

                # Keep the last incomplete line in buffer
                buffer = lines[-1]

                # Yield all complete lines
                for line in lines[:-1]:
                    # Add newline back since we split by it
                    yield line + b'\n'

            except Exception as e:
                logger.error(f"[{request_id}] Error processing chunk in legacy stream: {e}")
                # Continue reading despite errors
                continue

        # End of stream - yield any remaining data in buffer
        if buffer:
            logger.debug(f"[{request_id}] Yielding final buffer: {len(buffer)} bytes")
            yield buffer

    except aiohttp.client_exceptions.ClientPayloadError as e:
        # This error should not happen with iter_any(), but handle it just in case
        logger.warning(f"[{request_id}] Payload error in legacy mode (ending stream): {e}")
        if buffer:
            yield buffer
    except Exception as e:
        logger.error(f"[{request_id}] Fatal error in legacy stream reader: {e}")
        if buffer:
            yield buffer
        raise

async def handle_request(request: web.Request):
    """
    Main request handler with automatic retry for legacy API support.

    Features:
    - Extract model name from request to check if legacy mode needed
    - Automatically retry with legacy mode on ClientPayloadError
    - Transparent retry without client intervention
    """
    # Generate unique request ID for correlation
    request_id = str(uuid.uuid4())[:8]
    target_url = f"{TARGET_HOST}{request.rel_url}"
    logger.debug(f"[{request_id}] --> {request.method} {request.rel_url}")

    # Create request state
    request_states[request_id] = RequestState(request_id=request_id)

    # Start buffer cleanup task
    cleanup_task = asyncio.create_task(periodic_cleanup(request_id))

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "transfer-encoding", "connection")}

    data = await request.read() if request.can_read_body else None
    if data and fix_engine.get_setting('detailed_logging', True):
        logger.debug(f"[{request_id}] Request body ({len(data)} bytes): {data[:500]!r}")

    # Extract model name and check if we should use legacy mode
    model_name = extract_model_from_request(data)
    force_legacy = should_use_legacy_mode_for_model(model_name)

    if model_name and force_legacy:
        legacy_models = fix_engine.get_setting('legacy_models', [])
        if model_name in legacy_models:
            console_logger.info(f"[{request_id}] 🔄 Using legacy mode for model: {model_name}")
            logger.info(f"[{request_id}] Model {model_name} configured for legacy API mode")

    # Track retry attempts
    retry_count = 0
    max_retries = 1 if fix_engine.get_setting('auto_retry_legacy', True) else 0
    global legacy_mode_detected

    try:
        while retry_count <= max_retries:
            # Determine if we should use legacy mode for this attempt
            use_legacy_mode = force_legacy or legacy_mode_detected

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(method=request.method, url=target_url,
                                               headers=headers, data=data, allow_redirects=False) as resp:
                        logger.debug(f"[{request_id}] <-- {resp.status} {resp.reason} from backend (attempt {retry_count + 1})")

                        # Prepare response but don't send yet
                        response = web.StreamResponse(status=resp.status, reason=resp.reason, headers=resp.headers)
                        for hop in ("transfer-encoding", "connection", "content-length"):
                            response.headers.pop(hop, None)
                        await response.prepare(request)

                        # Choose the appropriate stream reader
                        if use_legacy_mode:
                            stream_iterator = read_legacy_stream(resp, request_id)
                        else:
                            stream_iterator = resp.content

                        # Process and forward the stream
                        async for raw_line in stream_iterator:
                            try:
                                line = raw_line.decode("utf-8")
                            except UnicodeDecodeError:
                                await response.write(raw_line)
                                continue

                            if not line.startswith("data:"):
                                await response.write(raw_line)
                                continue

                            payload = line[len("data:"):].strip()
                            if payload == "[DONE]":
                                # Process any remaining incomplete buffers before cleanup
                                await process_remaining_buffers(request_id, response)
                                logger.debug(f"[{request_id}] Stream ended, cleaning up buffers")
                                await cleanup_request(request_id)
                                await response.write(raw_line)
                                continue

                            try:
                                event = json.loads(payload)
                            except json.JSONDecodeError as e:
                                logger.warning(f"[{request_id}] Invalid JSON in SSE: {e}")
                                await response.write(raw_line)
                                continue

                            try:
                                fixed_event = await process_sse_event(event, request_id)
                                new_payload = json.dumps(fixed_event, ensure_ascii=False)

                                # Log detailed SSE output for debugging (file only)
                                logger.debug(f"[{request_id}] SSE Event: {json.dumps(fixed_event, indent=2)}")
                                if "tool_calls" in fixed_event.get("choices", [{}])[0].get("delta", {}):
                                    tool_calls = fixed_event["choices"][0]["delta"]["tool_calls"]
                                    for i, tool_call in enumerate(tool_calls):
                                        logger.debug(f"[{request_id}] SSE Tool Call {i}: {json.dumps(tool_call, indent=2)}")

                                await response.write(f"data: {new_payload}\n\n".encode("utf-8"))
                            except aiohttp.client_exceptions.ClientConnectionResetError:
                                logger.warning(f"[{request_id}] Client connection reset, stopping stream")
                                break
                            except Exception as e:
                                logger.error(f"[{request_id}] Error processing SSE event: {e}")
                                # Write original event on processing error
                                await response.write(raw_line)

                        await response.write_eof()
                        return response

            except aiohttp.client_exceptions.ClientPayloadError as e:
                # Legacy API detected
                if retry_count < max_retries and not use_legacy_mode:
                    # Enable legacy mode and retry
                    logger.error(f"[{request_id}] ClientPayloadError detected on attempt {retry_count + 1}: {e}")
                    console_logger.info(f"[{request_id}] 🔄 Legacy API detected, retrying with compatible mode...")
                    legacy_mode_detected = True
                    retry_count += 1

                    # Wait a bit before retry
                    await asyncio.sleep(0.1)
                    continue
                else:
                    # Already tried with legacy mode or retries disabled
                    logger.error(f"[{request_id}] ClientPayloadError in legacy mode: {e}")
                    return web.Response(
                        status=502,
                        text=f"Backend server error: {str(e)}"
                    )

            # If we reach here without exception, request succeeded
            break

    except aiohttp.client_exceptions.ServerDisconnectedError:
        logger.info(f"[{request_id}] Backend server disconnected - this is normal when client interrupts")
        return web.Response(status=502, text="Backend server disconnected")
    except aiohttp.client_exceptions.ClientConnectionResetError:
        logger.info(f"[{request_id}] Client connection reset - this is normal when client disconnects")
        return web.Response(status=499, text="Client disconnected")
    except Exception as e:
        logger.error(f"[{request_id}] Request handling error: {e}")
        raise
    finally:
        # Clean up request state and cancel cleanup task
        cleanup_task.cancel()
        try:
            await cleanup_request(request_id)
        except Exception as cleanup_error:
            logger.warning(f"[{request_id}] Cleanup error: {cleanup_error}")

async def periodic_cleanup(request_id: str):
    """Periodically clean up expired buffers for a request"""
    timeout = fix_engine.get_setting('buffer_timeout', 30)
    
    while request_id in request_states:
        try:
            await asyncio.sleep(timeout // 3)  # Check every 1/3 of timeout period
            if request_id in request_states:
                request_states[request_id].cleanup_expired_buffers(timeout)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[{request_id}] Cleanup task error: {e}")

async def cleanup_request(request_id: str):
    """Clean up all resources for a request"""
    if request_id in request_states:
        buffer_count = len(request_states[request_id].tool_buffers)
        if buffer_count > 0:
            logger.debug(f"[{request_id}] Cleaning up {buffer_count} tool buffers")
        del request_states[request_id]

async def process_sse_event(event: dict, request_id: str) -> dict:
    """Intercept delta.tool_calls and accumulate arguments, then fix when complete."""
    if request_id not in request_states:
        logger.warning(f"[{request_id}] Request state not found")
        return event
    
    request_state = request_states[request_id]

    if "choices" not in event or not event["choices"]:
        return event

    choice = event["choices"][0]
    delta = choice.get("delta", {})
    finish_reason = choice.get("finish_reason")
    
    # Accumulate content and check for XML-format tool calls
    content = delta.get("content", "")
    if content:
        # Add content to buffer
        request_state.content_buffer += content
        
        # Check if we have a complete XML tool call
        xml_tool_call = detect_and_convert_xml_tool_call(request_state.content_buffer)
        if xml_tool_call:
            console_logger.info(f"[{request_id}] 🔀 XML→JSON: {xml_tool_call['function_name']}")
            logger.info(f"[{request_id}] Detected XML tool call, converting to JSON format")
            
            # Create proper JSON tool call format
            fixed_call_id = f"call_{uuid.uuid4().hex[:24]}"
            args_str = json.dumps(xml_tool_call["arguments"], ensure_ascii=False)
            
            # Replace content with tool_calls
            delta["tool_calls"] = [{
                "index": 0,
                "id": fixed_call_id,
                "function": {
                    "name": xml_tool_call["function_name"],
                    "arguments": args_str
                }
            }]
            # Remove the XML content to prevent display
            delta["content"] = ""
            
            # Clear the content buffer since we processed the tool call
            request_state.content_buffer = ""
            
            logger.debug(f"[{request_id}] Converted XML to JSON tool call: {xml_tool_call['function_name']}")
        else:
            # Check if buffer is getting too large and clear it periodically
            max_buffer_size = fix_engine.get_setting('max_buffer_size', 1048576)
            if len(request_state.content_buffer) > max_buffer_size:
                logger.warning(f"[{request_id}] Content buffer exceeded size limit, clearing")
                request_state.content_buffer = ""
    
    if "tool_calls" not in delta:
        # Check if we need to process buffers on finish_reason
        if finish_reason == "tool_calls":
            await process_all_buffers(request_state, request_id)
        return event

    # Use a single buffer for all indexed tool call fragments
    main_buffer_key = "main_tool_call"
    
    # Collect all tool call fragments in this SSE event
    fragments_in_event = []
    named_tool_calls = []
    
    for tool in delta["tool_calls"]:
        tool_index = tool.get("index")
        func = tool.get("function", {})
        call_id = tool.get("id")
        tool_name = func.get("name", "")
        
        if call_id and tool_name and func.get("arguments", "").strip():
            # This is a complete named tool call with actual arguments
            named_tool_calls.append(tool)
        elif call_id and tool_name and not func.get("arguments", "").strip():
            # This is a named tool call header with empty arguments - likely for fragments
            # Remove it from the delta to prevent it from being sent
            logger.debug(f"[{request_id}] Suppressing empty named tool call header: {call_id}")
            # Mark this tool call for removal
            tool["_suppress"] = True
        elif "arguments" in func:
            # This is a fragment - collect it
            frag = func["arguments"]
            fragments_in_event.append((tool_index or 0, frag, tool))
    
    # Process fragments first
    if fragments_in_event:
        # Initialize buffer if needed
        if main_buffer_key not in request_state.tool_buffers:
            request_state.tool_buffers[main_buffer_key] = ToolBuffer(
                call_id=main_buffer_key,
                request_id=request_id,
                tool_name=""
            )
        
        buffer = request_state.tool_buffers[main_buffer_key]
        
        # Sort fragments by index and add to buffer
        fragments_in_event.sort(key=lambda x: x[0])
        for _, frag, tool in fragments_in_event:
            buffer.update_content(frag)
        
        # Check buffer size limit
        max_size = fix_engine.get_setting('max_buffer_size', 1048576)
        if buffer.size() > max_size:
            logger.error(f"[{request_id}] Buffer {main_buffer_key} exceeded size limit")
            del request_state.tool_buffers[main_buffer_key]
            # Suppress all fragments since buffer is invalid
            delta["tool_calls"] = []
        else:
            if fix_engine.get_setting('detailed_logging', True):
                total_frag = ''.join([f[1] for f in fragments_in_event])
                logger.debug(f"[{request_id}] Buffer {main_buffer_key} += {total_frag!r} (total: {len(buffer.content)} chars)")
            
            # Try to determine tool name from buffer content
            if not buffer.tool_name and buffer.content:
                buffer.tool_name = infer_tool_name_from_content(buffer.content)
            
            # Check if tool call is complete now
            if buffer.content and is_json_complete(buffer.content):
                # Process the complete tool call and get fixed arguments
                final_tool_name, fixed_args = await get_fixed_arguments(buffer, request_id)
                if fixed_args and final_tool_name:
                    # Replace all fragment tool_calls with a single complete one
                    # Use the original call ID format that OpenCode expects
                    fixed_call_id = f"call_{uuid.uuid4().hex[:24]}"
                    delta["tool_calls"] = [{
                        "index": 0,  # Required by OpenCode
                        "id": fixed_call_id,
                        "function": {
                            "name": final_tool_name,
                            "arguments": fixed_args
                        }
                    }]
                    console_logger.info(f"[{request_id}] 🔧 Tool call: {final_tool_name}")
                    logger.info(f"[{request_id}] Replaced fragments with complete fixed tool call: {final_tool_name}")
                    del request_state.tool_buffers[main_buffer_key]
                else:
                    # Couldn't get fixed args or tool name, suppress fragments to prevent client errors
                    if not buffer.tool_name:
                        logger.warning(f"[{request_id}] Could not infer tool name from content: {buffer.content[:100]}...")
                    logger.warning(f"[{request_id}] Failed to get fixed args or tool name, suppressing fragments")
                    delta["tool_calls"] = []
            else:
                # Tool call incomplete, suppress fragments to prevent sending invalid data to client
                logger.debug(f"[{request_id}] Tool call incomplete, suppressing {len(fragments_in_event)} fragments")
                delta["tool_calls"] = []
    
    # Process named tool calls normally
    for tool in named_tool_calls:
        call_id = tool["id"]
        func = tool.get("function", {})
        tool_name = func.get("name", "")
        
        if call_id not in request_state.tool_buffers:
            request_state.tool_buffers[call_id] = ToolBuffer(
                call_id=call_id,
                request_id=request_id,
                tool_name=tool_name
            )
        
        buffer = request_state.tool_buffers[call_id]
        
        if "arguments" in func:
            frag = func["arguments"]
            buffer.update_content(frag)
            
            if fix_engine.get_setting('detailed_logging', True):
                logger.debug(f"[{request_id}] Named buffer {call_id} ({buffer.tool_name}) += {frag!r} (total: {len(buffer.content)} chars)")
            
            if buffer.content and is_json_complete(buffer.content):
                console_logger.info(f"[{request_id}] 🔧 Tool call: {buffer.tool_name}")
                await process_complete_buffer(buffer, tool, request_id)
                del request_state.tool_buffers[call_id]

    # Check for finish_reason indicating all tool calls are done
    if finish_reason == "tool_calls":
        await process_all_buffers(request_state, request_id)

    # Remove suppressed tool calls from the event
    if "tool_calls" in delta:
        delta["tool_calls"] = [tool for tool in delta["tool_calls"] if not tool.get("_suppress")]
        # If all tool calls were suppressed, remove the tool_calls key entirely
        if not delta["tool_calls"]:
            del delta["tool_calls"]

    return event

async def process_complete_buffer(buffer: ToolBuffer, tool: dict, request_id: str):
    """Process a complete tool call buffer"""
    full_args_str = buffer.content
    tool_name = buffer.tool_name
    call_id = buffer.call_id
    
    try:
        args_obj = json.loads(full_args_str)
        final_tool_name, args_obj = fix_engine.apply_fixes(tool_name, args_obj, request_id)
        fixed_args_str = json.dumps(args_obj, ensure_ascii=False)
        
        # Update tool name if it was converted
        if final_tool_name != tool_name:
            tool["function"]["name"] = final_tool_name
            logger.debug(f"[{request_id}] Converted tool call {call_id} ({tool_name}→{final_tool_name}): {len(fixed_args_str)} chars")
        else:
            logger.debug(f"[{request_id}] Fixed tool call {call_id} ({tool_name}): {len(fixed_args_str)} chars")
        
        if fix_engine.get_setting('detailed_logging', True):
            logger.debug(f"[{request_id}] Fixed args: {fixed_args_str}")
        
        tool["function"]["arguments"] = fixed_args_str
    except json.JSONDecodeError as e:
        logger.warning(f"[{request_id}] JSON parse failed for {call_id}: {e}")
        # Try to recover with fallback
        if await try_json_recovery(full_args_str, tool, tool_name, request_id):
            logger.info(f"[{request_id}] Successfully recovered malformed JSON for {call_id}")
        else:
            # Keep original if recovery fails
            logger.warning(f"[{request_id}] JSON recovery failed, keeping original")
    except Exception as e:
        logger.error(f"[{request_id}] Failed to process tool call {call_id}: {e}")

async def process_all_buffers(request_state: RequestState, request_id: str):
    """Process all remaining buffers when tool_calls finish_reason is received"""
    if not request_state.tool_buffers:
        return
        
    logger.info(f"[{request_id}] Processing {len(request_state.tool_buffers)} remaining buffers")
    
    for call_id, buffer in list(request_state.tool_buffers.items()):
        if buffer.content:
            # Create a dummy tool structure for processing
            dummy_tool = {
                "id": call_id,
                "function": {
                    "name": buffer.tool_name,
                    "arguments": buffer.content
                }
            }
            await process_complete_buffer(buffer, dummy_tool, request_id)
            # Log the final processed arguments
            final_args = dummy_tool["function"]["arguments"]
            logger.info(f"[{request_id}] Final processed args for {call_id}: {final_args}")
    
    # Clear all buffers after processing
    request_state.tool_buffers.clear()

async def process_remaining_buffers(request_id: str, response):
    """Process any remaining incomplete buffers before stream end"""
    if request_id not in request_states:
        return
        
    request_state = request_states[request_id]
    if not request_state.tool_buffers:
        return
    
    logger.info(f"[{request_id}] Processing {len(request_state.tool_buffers)} incomplete buffers before cleanup")
    
    for call_id, buffer in list(request_state.tool_buffers.items()):
        if buffer.content:
            try:
                # Try to fix incomplete JSON
                fixed_json = await try_fix_incomplete_json(buffer.content)
                if fixed_json and buffer.tool_name:
                    args_obj = json.loads(fixed_json)
                    final_tool_name, args_obj = fix_engine.apply_fixes(buffer.tool_name, args_obj, request_id)
                    fixed_args_str = json.dumps(args_obj, ensure_ascii=False)
                    
                    # Create a completion event for this tool call
                    # Generate a proper call ID format
                    completion_call_id = f"call_{uuid.uuid4().hex[:24]}"
                    completion_event = {
                        "choices": [{
                            "delta": {
                                "tool_calls": [{
                                    "index": 0,  # Required by OpenCode
                                    "id": completion_call_id,
                                    "function": {
                                        "name": final_tool_name,
                                        "arguments": fixed_args_str
                                    }
                                }]
                            }
                        }]
                    }
                    
                    new_payload = json.dumps(completion_event, ensure_ascii=False)
                    try:
                        await response.write(f"data: {new_payload}\n\n".encode("utf-8"))
                        console_logger.info(f"[{request_id}] 🔧 Completion: {final_tool_name}")
                        logger.info(f"[{request_id}] Sent completion for incomplete buffer {call_id}")
                    except Exception as write_error:
                        logger.warning(f"[{request_id}] Failed to write completion: {write_error}")
                elif not buffer.tool_name:
                    logger.warning(f"[{request_id}] Skipping completion for buffer {call_id} - could not infer tool name from: {buffer.content[:100]}...")
                        
            except Exception as e:
                logger.warning(f"[{request_id}] Failed to process incomplete buffer {call_id}: {e}")

async def try_fix_incomplete_json(json_str: str) -> str:
    """Try to fix incomplete JSON by adding missing closing braces/brackets"""
    if not json_str.strip():
        return ""
    
    json_str = json_str.strip()
    
    # Count unmatched braces and brackets
    open_braces = json_str.count('{')
    close_braces = json_str.count('}')
    open_brackets = json_str.count('[')
    close_brackets = json_str.count(']')
    
    # Add missing closing characters
    result = json_str
    result += ']' * (open_brackets - close_brackets)
    result += '}' * (open_braces - close_braces)
    
    # Validate the result
    try:
        json.loads(result)
        return result
    except:
        return None

async def try_json_recovery(malformed_json: str, tool: dict, tool_name: str, request_id: str) -> bool:
    """Attempt to recover from malformed JSON"""
    recovery_attempts = [
        # Try to fix common JSON issues
        lambda s: s.rstrip(',') + '}',  # Remove trailing comma and add closing brace
        lambda s: s + '}',  # Just add closing brace
        lambda s: '{' + s + '}' if not s.startswith('{') else s,  # Add opening brace
    ]
    
    for i, fix_func in enumerate(recovery_attempts):
        try:
            fixed_json = fix_func(malformed_json.strip())
            args_obj = json.loads(fixed_json)
            final_tool_name, args_obj = fix_engine.apply_fixes(tool_name, args_obj, request_id)
            fixed_args_str = json.dumps(args_obj, ensure_ascii=False)
            tool["function"]["name"] = final_tool_name
            tool["function"]["arguments"] = fixed_args_str
            logger.info(f"[{request_id}] JSON recovery attempt {i+1} succeeded")
            return True
        except:
            continue
    
    return False

def is_json_complete(json_str: str) -> bool:
    """Robust detection if JSON string is syntactically complete"""
    if not json_str or not json_str.strip():
        return False
    
    json_str = json_str.strip()
    
    # Must start with { or [
    if not json_str.startswith(('{', '[')):
        return False
    
    # Check bracket/brace balancing
    stack = []
    in_string = False
    escape_next = False
    
    for char in json_str:
        if escape_next:
            escape_next = False
            continue
            
        if char == '\\':
            escape_next = True
            continue
            
        if char == '"' and not escape_next:
            in_string = not in_string
            continue
            
        if in_string:
            continue
            
        if char in '{[':
            stack.append(char)
        elif char in '}]':
            if not stack:
                return False
            
            last = stack.pop()
            if (char == '}' and last != '{') or (char == ']' and last != '['):
                return False
    
    # JSON is complete if stack is empty (all brackets matched) and not in string
    return len(stack) == 0 and not in_string

def validate_json_syntax(json_str: str) -> bool:
    """Quick validation that JSON is syntactically correct"""
    try:
        json.loads(json_str)
        return True
    except json.JSONDecodeError:
        return False

def infer_tool_name_from_content(content: str) -> str:
    """Infer tool name from JSON content by looking for known parameters"""
    if not content:
        return ""
    
    # Check for known parameter patterns (simple string matching first)
    if '"todos"' in content:
        return "todowrite"
    elif '"command"' in content and not '"edits"' in content:
        return "bash"
    elif '"file_path"' in content and '"edits"' in content:
        return "multiedit"
    elif '"filePath"' in content and '"oldString"' in content and '"newString"' in content:
        return "edit"
    elif '"file_path"' in content and '"old_string"' in content and '"new_string"' in content:
        return "edit"
    elif '"pattern"' in content and '"output_mode"' in content:
        return "grep"
    elif '"pattern"' in content:
        return "glob"  # Default pattern parameter to glob tool
    elif '"url"' in content and '"prompt"' in content:
        return "webfetch"
    elif '"query"' in content:
        return "websearch"
    elif '"content"' in content and ('"file_path"' in content or '"filePath"' in content):
        return "write"
    elif '"file_path"' in content or '"filePath"' in content:
        return "read"  # Default file path parameter to read tool
    elif '"description"' in content and '"prompt"' in content and '"subagent_type"' in content:
        return "task"
    elif '"notebook_path"' in content and '"new_source"' in content:
        return "notebookedit"
    elif '"path"' in content:
        return "list"  # Default path-only parameter to list tool (directory listing)
    
    return ""  # Unknown tool

def detect_and_convert_xml_tool_call(content: str) -> dict:
    """
    Detect XML-format tool calls like <function=glob><parameter=pattern>*.py</parameter></function>
    and convert them to OpenAI-compatible JSON format.
    Handles both single and multiple parameters.
    """
    import re
    
    # First, extract the function name
    function_match = re.search(r'<function=([^>]+)>', content)
    if not function_match:
        return None
    
    function_name = function_match.group(1).strip()
    
    # Find all parameters within this function call
    # Pattern to match all parameters: <parameter=name>value</parameter>
    param_pattern = r'<parameter=([^>]+)>\s*([^<]*?)\s*</parameter>'
    param_matches = re.findall(param_pattern, content, re.DOTALL)
    
    if not param_matches:
        return None
    
    # Create JSON arguments object from all parameters
    args_obj = {}
    for param_name, param_value in param_matches:
        args_obj[param_name.strip()] = param_value.strip()
    
    return {
        "function_name": function_name,
        "arguments": args_obj
    }

async def get_fixed_arguments(buffer: ToolBuffer, request_id: str) -> tuple[str, str]:
    """Get fixed arguments from buffer and return as (tool_name, JSON string)"""
    try:
        args_obj = json.loads(buffer.content)
        final_tool_name, args_obj = fix_engine.apply_fixes(buffer.tool_name, args_obj, request_id)
        return final_tool_name, json.dumps(args_obj, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[{request_id}] Failed to get fixed arguments: {e}")
        return buffer.tool_name, ""

async def health_check(request: web.Request):
    """Health check endpoint"""
    stats = {
        'status': 'healthy',
        'active_requests': len(request_states),
        'total_buffers': sum(len(state.tool_buffers) for state in request_states.values()),
        'config_loaded': bool(fix_engine.config),
        'target_host': TARGET_HOST,
        'legacy_mode': fix_engine.get_setting('legacy_api_mode', False) or legacy_mode_detected,
        'legacy_mode_auto_detected': legacy_mode_detected,
        'legacy_models': fix_engine.get_setting('legacy_models', []),
        'auto_retry_legacy': fix_engine.get_setting('auto_retry_legacy', True),
        'uptime': 'unknown'  # Could track start time
    }
    return web.json_response(stats)

async def reload_config(request: web.Request):
    """Reload configuration endpoint"""
    try:
        global fix_engine
        fix_engine = ToolFixEngine(CONFIG_FILE)
        return web.json_response({'status': 'success', 'message': 'Configuration reloaded'})
    except Exception as e:
        logger.error(f"Failed to reload config: {e}")
        return web.json_response({'status': 'error', 'message': str(e)}, status=500)

def main():
    """Main entry point for the proxy server"""
    app = web.Application()
    
    # Add health and management endpoints
    app.router.add_get('/_health', health_check)
    app.router.add_post('/_reload', reload_config)
    
    # Main proxy route (catch-all)
    app.router.add_route("*", "/{tail:.*}", handle_request)

    console_logger.info(f"🔌 Qwen3 Call Patch Proxy starting...")
    console_logger.info(f"   📡 Listening: 0.0.0.0:{LISTEN_PORT}")
    console_logger.info(f"   🎯 Target: {TARGET_HOST}")
    console_logger.info(f"   ⚙️  Config: {CONFIG_FILE}")

    # Show legacy mode status
    legacy_mode = fix_engine.get_setting('legacy_api_mode', False)
    auto_detect = fix_engine.get_setting('auto_detect_legacy', True)
    if legacy_mode:
        console_logger.info(f"   🔄 Legacy API mode: ENABLED (forced)")
    elif auto_detect:
        console_logger.info(f"   🔄 Legacy API mode: AUTO-DETECT enabled")
    else:
        console_logger.info(f"   🔄 Legacy API mode: DISABLED")

    logger.info(f"   Health check: http://localhost:{LISTEN_PORT}/_health")
    logger.info(f"   Reload config: POST http://localhost:{LISTEN_PORT}/_reload")
    
    try:
        web.run_app(app, host="0.0.0.0", port=LISTEN_PORT)
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
    finally:
        # Cleanup any remaining state
        request_states.clear()

if __name__ == "__main__":
    main()
