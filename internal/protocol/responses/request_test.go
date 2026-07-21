package responses

import (
	"strings"
	"testing"
)

func TestBuildChatBodyConvertsResponsesInput(t *testing.T) {
	body := BuildChatBody(map[string]any{
		"instructions":        "be useful",
		"input":               []any{map[string]any{"type": "input_text", "text": "hi"}, map[string]any{"type": "function_call_output", "call_id": "call_1", "output": map[string]any{"ok": true}}},
		"max_output_tokens":   64,
		"tools":               []any{map[string]any{"type": "function", "name": "Edit", "parameters": map[string]any{"type": "object"}}},
		"tool_choice":         "required",
		"parallel_tool_calls": true,
		"reasoning":           map[string]any{"effort": "medium"},
		"metadata":            map[string]any{"user": "u1", "prompt_cache_key": "pck"},
	}, "grok")
	if body["model"] != "grok" || body["max_tokens"] != 64 || body["tool_choice"] != "required" || body["reasoning_effort"] != "medium" || body["user"] != "u1" || body["prompt_cache_key"] != "pck" {
		t.Fatalf("unexpected body %#v", body)
	}
	messages := body["messages"].([]map[string]any)
	if len(messages) != 3 {
		t.Fatalf("messages = %#v", messages)
	}
	if messages[0]["role"] != "system" || messages[1]["content"] != "hi" || messages[2]["role"] != "tool" || messages[2]["tool_call_id"] != "call_1" {
		t.Fatalf("unexpected messages %#v", messages)
	}
	tools := body["tools"].([]any)
	fn := tools[0].(map[string]any)["function"].(map[string]any)
	if fn["name"] != "Edit" {
		t.Fatalf("tools = %#v", tools)
	}
}

func TestBuildChatBodyConvertsCustomToolToUpstreamFunction(t *testing.T) {
	body := BuildChatBody(map[string]any{
		"input": []any{map[string]any{"role": "user", "content": "patch the file"}},
		"tools": []any{map[string]any{
			"type":        "custom",
			"name":        "apply_patch",
			"description": "Apply a patch to files.",
			"format":      map[string]any{"type": "grammar", "syntax": "lark", "definition": "start: /[\\s\\S]+/"},
		}},
	}, "grok")

	tools, ok := body["tools"].([]any)
	if !ok || len(tools) != 1 {
		t.Fatalf("custom tool was dropped: %#v", body["tools"])
	}
	fn, _ := tools[0].(map[string]any)["function"].(map[string]any)
	if fn == nil || fn["name"] != "apply_patch" {
		t.Fatalf("unexpected upstream tool: %#v", tools[0])
	}
	if !strings.Contains(stringValue(fn["description"]), "start: /[\\s\\S]+/") {
		t.Fatalf("custom grammar was not preserved for upstream: %#v", fn["description"])
	}
	params, _ := fn["parameters"].(map[string]any)
	props, _ := params["properties"].(map[string]any)
	input, _ := props["input"].(map[string]any)
	if input["type"] != "string" {
		t.Fatalf("custom input schema not converted to a string: %#v", params)
	}
}

func TestInputToMessagesConvertsCustomToolHistory(t *testing.T) {
	messages := InputToMessages([]any{
		map[string]any{
			"type":    "custom_tool_call",
			"call_id": "call_patch",
			"name":    "apply_patch",
			"input":   "*** Begin Patch\n*** End Patch\n",
		},
		map[string]any{
			"type":    "custom_tool_call_output",
			"call_id": "call_patch",
			"output":  "Success",
		},
	}, "")

	if len(messages) != 2 {
		t.Fatalf("custom tool history was dropped: %#v", messages)
	}
	calls, _ := messages[0]["tool_calls"].([]any)
	if len(calls) != 1 {
		t.Fatalf("custom call not converted: %#v", messages[0])
	}
	fn, _ := calls[0].(map[string]any)["function"].(map[string]any)
	if fn["name"] != "apply_patch" || !strings.Contains(stringValue(fn["arguments"]), `"input"`) {
		t.Fatalf("unexpected custom call conversion: %#v", fn)
	}
	if messages[1]["role"] != "tool" || messages[1]["tool_call_id"] != "call_patch" {
		t.Fatalf("unexpected custom output conversion: %#v", messages[1])
	}
}

func TestBuildObjectConvertsChatResult(t *testing.T) {
	obj := BuildObject("resp_1", "grok", "hello", "plan", []map[string]any{{"id": "call_1", "function": map[string]any{"name": "Edit", "arguments": "{\"file_path\":\"/x\"}"}}}, map[string]any{"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}, 123, "resp_0", map[string]any{"a": "b"})
	if obj["id"] != "resp_1" || obj["status"] != "completed" || obj["previous_response_id"] != "resp_0" {
		t.Fatalf("unexpected object %#v", obj)
	}
	output := obj["output"].([]any)
	if len(output) != 2 {
		t.Fatalf("output = %#v", output)
	}
	msg := output[0].(map[string]any)
	call := output[1].(map[string]any)
	if msg["type"] != "message" || call["type"] != "function_call" || call["call_id"] != "call_1" {
		t.Fatalf("unexpected output %#v", output)
	}
	usage := obj["usage"].(map[string]any)
	if usage["input_tokens"] != 2 || usage["output_tokens"] != 3 || usage["total_tokens"] != 5 {
		t.Fatalf("usage = %#v", usage)
	}
}

func TestBuildObjectRestoresCustomToolCall(t *testing.T) {
	obj := BuildObject(
		"resp_custom",
		"grok",
		"",
		"",
		[]map[string]any{{
			"id":   "call_patch",
			"type": "custom",
			"function": map[string]any{
				"name":      "apply_patch",
				"arguments": `{"input":"*** Begin Patch\n*** End Patch\n"}`,
			},
		}},
		map[string]any{},
		123,
		"",
		nil,
	)
	output := obj["output"].([]any)
	if len(output) != 1 {
		t.Fatalf("unexpected custom output: %#v", output)
	}
	call := output[0].(map[string]any)
	if call["type"] != "custom_tool_call" || call["name"] != "apply_patch" || call["input"] != "*** Begin Patch\n*** End Patch\n" {
		t.Fatalf("custom tool was not restored: %#v", call)
	}
	if call["arguments"] != nil {
		t.Fatalf("custom tool leaked function arguments: %#v", call)
	}
}

func TestInputToMessagesFlattensInputTextParts(t *testing.T) {
	messages := InputToMessages([]any{
		map[string]any{
			"role": "user",
			"content": []any{
				map[string]any{"type": "input_text", "text": "hi"},
			},
		},
	}, "be useful")
	if len(messages) != 2 {
		t.Fatalf("messages = %#v", messages)
	}
	if messages[0]["role"] != "system" || messages[0]["content"] != "be useful" {
		t.Fatalf("system = %#v", messages[0])
	}
	if messages[1]["role"] != "user" || messages[1]["content"] != "hi" {
		t.Fatalf("user content not flattened: %#v", messages[1])
	}
}

func TestInputToMessagesDropsEmptyContentBlocks(t *testing.T) {
	messages := InputToMessages([]any{
		map[string]any{"role": "user", "content": []any{
			map[string]any{"type": "input_text", "text": ""},
		}},
		map[string]any{"role": "user", "content": []any{
			map[string]any{"type": "input_text", "text": "ok"},
			map[string]any{"type": "input_text", "text": ""},
		}},
		map[string]any{"role": "assistant", "content": ""},
	}, "")
	// empty user dropped, empty assistant dropped, mixed user keeps only "ok"
	if len(messages) != 1 {
		t.Fatalf("messages=%#v", messages)
	}
	if messages[0]["content"] != "ok" {
		t.Fatalf("content=%#v", messages[0]["content"])
	}
}

func TestInputToMessagesKeepsToolOnlyAssistant(t *testing.T) {
	messages := InputToMessages([]any{
		map[string]any{
			"role":    "assistant",
			"content": "",
			"tool_calls": []any{
				map[string]any{"id": "c1", "type": "function", "function": map[string]any{"name": "shell", "arguments": "{}"}},
			},
		},
	}, "")
	if len(messages) != 1 {
		t.Fatalf("messages=%#v", messages)
	}
	if messages[0]["content"] != nil {
		t.Fatalf("tool-only assistant content should be nil, got %#v", messages[0]["content"])
	}
}
