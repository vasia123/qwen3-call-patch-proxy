# Qwen3 Call Patch Proxy - Detailed Guide

## Problem Statement

Qwen3-Coder models generate tool calls with format inconsistencies that cause errors in OpenCode:
- **String parameters instead of arrays** (e.g., `todos` parameter as JSON string instead of array)  
- **String booleans instead of actual booleans** (e.g., `"True"` instead of `true`)
- **Fragmented tool calls** sent across multiple Server-Sent Events (SSE)
- **Missing required parameters** for various tools
- **Inconsistent parameter naming** (camelCase vs snake_case)

## Solution Details

This proxy intercepts HTTP requests between your Qwen3 model server and OpenCode, automatically:

✅ **Consolidates fragmented tool calls** across multiple SSE events  
✅ **Converts XML-format tool calls to JSON** when streamed across multiple events  
✅ **Converts string parameters to proper types** (string→array, string→boolean)  
✅ **Generates proper tool call IDs** in OpenCode format (`call_<24_hex_chars>`)  
✅ **Adds missing required parameters** with sensible defaults  
✅ **Handles various tool types** (TodoWrite, Edit, Bash, Task, MultiEdit, etc.)  
✅ **Provides comprehensive logging** for debugging

## Supported Tools

Available tools (all lowercase as per OpenCode specification):

| Tool | Description | Fixes Applied |
|------|-------------|---------------|
| **bash** | Execute shell commands | Add default `description` if missing |
| **edit** | Modify existing files | Convert `replaceAll`/`replace_all` string to boolean |
| **write** | Create new files | Handle file path and content parameters |
| **read** | Read file contents | Convert to `write` when `content` provided (LLM confusion fix) |
| **grep** | Search file contents | Validate `output_mode` enum values |
| **glob** | Find files by pattern | Add default `path` if missing |
| **list** | List directory contents | Handle path parameter |
| **patch** | Apply patches to files | Handle patch parameters |
| **todowrite** | Manage todo lists | Convert `todos` string to array, fix malformed JSON (single quotes→double quotes) |
| **todoread** | Read todo lists | Handle todo parameters |
| **webfetch** | Fetch web content | Handle URL and prompt parameters |
| **task** | Execute tasks with agents | Add default `subagent_type` if missing |
| **multiedit** | Multiple file edits | Convert `edits` string to array |

## Examples

### Before (Qwen3 Output)
```json
{
  "tool_calls": [
    {"index": 1, "function": {"arguments": "{"}},
    {"index": 2, "function": {"arguments": "\"todos\": \"[{\\\"content\\\": \\\"Test\\\", \\\"id\\\": \\\"1\\\"}]\""}},
    {"index": 3, "function": {"arguments": "}"}}
  ]
}
```

### After (Proxy Output)
```json
{
  "tool_calls": [{
    "index": 0,
    "id": "call_f9349827809c44ce8334a0d5",
    "function": {
      "name": "todowrite",
      "arguments": "{\"todos\": [{\"content\": \"Test\", \"id\": \"1\"}]}"
    }
  }]
}
```

## Configuration

The proxy uses `tool_fixes.yaml` for configuration:

```yaml
tools:
  todowrite:
    fixes:
      - name: "todos_string_to_array"
        parameter: "todos"
        condition: "is_string"
        action: "parse_json_array"
        fallback_value: []

  edit:
    fixes:
      - name: "replace_all_string_to_boolean"
        parameter: "replaceAll"
        condition: "is_string" 
        action: "convert_string_to_boolean"

settings:
  buffer_timeout: 120        # Timeout for tool call fragments
  max_buffer_size: 1048576  # Maximum buffer size (1MB)
  detailed_logging: true    # Enable debug logging
```

### Fix Actions
- `parse_json_array` - Parse JSON string into array
- `parse_json_object` - Parse JSON string into object  
- `convert_string_to_boolean` - Convert string to boolean
- `set_default` - Set default value if missing
- `remove_parameter` - Remove unwanted parameter from tool call
- `convert_tool_to_write` - Convert read+content calls to write calls (LLM intent fix)

### Fix Conditions
- `is_string` - Parameter is a string
- `missing` - Parameter is missing
- `missing_or_empty` - Parameter is missing or empty
- `exists` - Parameter exists (opposite of missing)
- `invalid_enum` - Parameter not in valid values list

## Monitoring & Health Checks

- **Health check:** `GET http://localhost:7999/_health`
- **Reload config:** `POST http://localhost:7999/_reload`

### Logging Structure

The proxy uses a **dual logging system**:

**Console Output (clean, easy to follow):**
- 🔧 Tool calls and adaptations 
- 🔀 XML to JSON conversions
- ⚙️ Configuration changes
- 📡 Server status messages

**Detailed Logs (`./logs/proxy_detailed.log`):**
- Complete HTTP request/response communication
- SSE event streams with full JSON
- Buffer management and fragment processing
- Debug traces with function names and line numbers

```bash
# Start the proxy (clean console output)
python call_patch_proxy.py

# Monitor detailed logs in another terminal
tail -f logs/proxy_detailed.log
```

Response example:
```json
{
  "status": "healthy",
  "active_requests": 2,
  "total_buffers": 5,
  "config_loaded": true,
  "target_host": "http://127.0.0.1:8080"
}
```

## Development

### Running Tests

```bash
# Test all functionality
python test_proxy.py
python test_fragment_processing.py  
python test_edit_boolean.py
python test_task_detection.py
```

### Adding New Tool Support

1. **Add tool detection** in `infer_tool_name_from_content()`:
   ```python
   elif '"your_param"' in content:
       return "your_tool_name"
   ```

2. **Add fix rules** in `tool_fixes.yaml`:
   ```yaml
   your_tool_name:
     fixes:
       - name: "your_fix_name"
         parameter: "param_name"
         condition: "is_string"
         action: "parse_json_array"
   ```

## Troubleshooting

### Common Issues

**Empty tool names:** Update tool inference function with new parameter patterns
**Fragment timeouts:** Increase `buffer_timeout` in config
**Memory usage:** Reduce `max_buffer_size` or enable more aggressive cleanup
**Connection errors:** Check that Qwen3 server is running on expected port

### Debug Mode

Enable detailed logging:
```python
LOG_LEVEL = logging.DEBUG  # In call_patch_proxy.py
```