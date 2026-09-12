// websocket.go owns everything about talking to loom/server.py — connecting,
// the background read loop, and sending human messages. main.go never touches
// the network directly; it only reacts to the Msg types defined here.
package main

import (
	"context"
	"encoding/json"
	"fmt"

	tea "charm.land/bubbletea/v2"
	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

// envelope is decoded first, on every incoming line, just to see what kind
// of event it is — the real payload gets decoded a second time below, into
// whichever struct actually matches.
type envelope struct {
	Type string `json:"type"`
}

type agentTurnMsg struct {
	SessionID string  `json:"session_id"`
	TaskID    *string `json:"task_id"`
	ModelKey  string  `json:"model_key"`
	Text      string  `json:"text"`
}

type toolCallMsg struct {
	SessionID string         `json:"session_id"`
	TaskID    *string        `json:"task_id"`
	Name      string         `json:"name"`
	Args      map[string]any `json:"args"`
}

type toolResultMsg struct {
	SessionID string  `json:"session_id"`
	TaskID    *string `json:"task_id"`
	Name      string  `json:"name"`
	Content   string  `json:"content"`
}

type sessionEndedMsg struct {
	SessionID string `json:"session_id"`
	Status    string `json:"status"`
	FinalText string `json:"final_text"`
}

type serverErrorMsg struct {
	Message string `json:"message"`
}

// approvalRequestedMsg means an agent thread is parked, waiting on an answer.
// It stays parked until we reply with this RequestID or the session ends —
// there's no timeout on the server side, deliberately.
type approvalRequestedMsg struct {
	SessionID   string `json:"session_id"`
	RequestID   string `json:"request_id"`
	ToolName    string `json:"tool_name"`
	Reason      string `json:"reason"`
	Description string `json:"description"`
}

// vaultEntry mirrors config/vault.py's VaultEntry dataclass. Validated is a
// pointer because the server sends null for "never checked this session" —
// which is meaningfully different from "checked and failed".
type vaultEntry struct {
	ModelKey  string `json:"model_key"`
	Tier      string `json:"tier"`
	Present   bool   `json:"present"`
	Validated *bool  `json:"validated"`
}

type modelsMsg struct {
	Models []vaultEntry `json:"models"`
}

// sessionStartedMsg arrives when a thread we opened has its session. The very
// first one is consumed inside connect() during the handshake, so any that
// reach listen() belong to a thread started later on the same connection.
type sessionStartedMsg struct {
	SessionID string `json:"session_id"`
}

// startThread opens a side conversation on the connection we already have.
// parentID seeds it with the parent's progress server-side; the reply comes
// back asynchronously through listen() as sessionStartedMsg.
func startThread(conn *websocket.Conn, question, parentID, modelKey string) error {
	return wsjson.Write(context.Background(), conn, map[string]any{
		"type": "start_session", "task": question, "model_key": modelKey,
		"mode": "agent", "parent_session_id": parentID,
	})
}

// storedEvent is one entry from a session's persisted log. The server keeps
// every emitted event in SessionState.turns; on attach it hands the whole list
// over so a fresh window can rebuild the transcript.
type storedEvent struct {
	Type    string         `json:"type"`
	Payload map[string]any `json:"payload"`
}

// attachedMsg is the reply to attach_session: the session's state, including
// its event history.
type attachedMsg struct {
	SessionID string `json:"session_id"`
	State     struct {
		Task     string        `json:"task"`
		ModelKey string        `json:"model_key"`
		Status   string        `json:"status"`
		Turns    []storedEvent `json:"turns"`
	} `json:"state"`
}

// attach joins a session that already exists instead of starting a new one.
// connect() always sends start_session, so without this every launch created a
// fresh conversation and a session you closed stayed unreachable for the life
// of the server.
func attach(url, sessionID string) (*websocket.Conn, attachedMsg, error) {
	var reply attachedMsg
	ctx := context.Background()
	conn, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		return nil, reply, fmt.Errorf("dial failed: %w", err)
	}
	if err := wsjson.Write(ctx, conn, map[string]any{
		"type": "attach_session", "session_id": sessionID,
	}); err != nil {
		return nil, reply, fmt.Errorf("failed to send attach_session: %w", err)
	}

	// Nothing else can arrive first: this connection has sent only that one
	// message, and the server answers it with "attached" or "error".
	_, data, err := conn.Read(ctx)
	if err != nil {
		return nil, reply, fmt.Errorf("failed to read attach reply: %w", err)
	}
	var probe struct {
		Type    string `json:"type"`
		Message string `json:"message"`
	}
	if json.Unmarshal(data, &probe) == nil && probe.Type == "error" {
		return nil, reply, fmt.Errorf("%s", probe.Message)
	}
	if err := json.Unmarshal(data, &reply); err != nil {
		return nil, reply, fmt.Errorf("malformed attach reply: %w", err)
	}
	return conn, reply, nil
}

// connectionLostMsg is ours, not the server's — listen() synthesizes it
// when the read itself fails, there's no wire format for it.
type connectionLostMsg struct {
	Err error
}

// connect dials the backend and completes the required start_session
// handshake — server.py's handler() expects start_session as the very first
// message on a connection, and replies with session_started before anything
// else. mode="agent" is a single chatting agent with real bash/file tools;
// "orchestrator" is the multi-agent delegation path from Phase 5.
func connect(url, task, modelKey, mode string) (*websocket.Conn, string, error) {
	ctx := context.Background()
	conn, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		return nil, "", fmt.Errorf("dial failed: %w", err)
	}

	err = wsjson.Write(ctx, conn, map[string]any{
		"type": "start_session", "task": task, "model_key": modelKey, "mode": mode,
	})
	if err != nil {
		return nil, "", fmt.Errorf("failed to send start_session: %w", err)
	}

	var reply struct {
		SessionID string `json:"session_id"`
	}
	if err := wsjson.Read(ctx, conn, &reply); err != nil {
		return nil, "", fmt.Errorf("failed to read session_started reply: %w", err)
	}

	return conn, reply.SessionID, nil
}

// listen runs on its own goroutine for the connection's whole life,
// translating each incoming line into a tea.Msg and handing it to the
// running program via p.Send(). It never touches the model directly —
// only the Elm loop (Update) is allowed to do that.
func listen(conn *websocket.Conn, p *tea.Program) {
	ctx := context.Background()
	for {
		_, data, err := conn.Read(ctx)
		if err != nil {
			p.Send(connectionLostMsg{Err: err})
			return
		}

		var env envelope
		if err := json.Unmarshal(data, &env); err != nil {
			p.Send(serverErrorMsg{Message: "malformed message from server: " + err.Error()})
			continue
		}

		switch env.Type {
		case "agent_turn":
			var m agentTurnMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		case "tool_call":
			var m toolCallMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		case "tool_result":
			var m toolResultMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		case "session_ended":
			var m sessionEndedMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		case "approval_requested":
			var m approvalRequestedMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		case "session_started":
			var m sessionStartedMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		case "models":
			var m modelsMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		case "error":
			var m serverErrorMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		}
	}
}

// sendApprovalResponse answers one pending approval. toolName is only needed
// when always is set — the server remembers "always" per tool, not per request.
func sendApprovalResponse(conn *websocket.Conn, sessionID, requestID, toolName string,
	approved, always bool) error {
	return wsjson.Write(context.Background(), conn, map[string]any{
		"type": "approval_response", "session_id": sessionID, "request_id": requestID,
		"tool_name": toolName, "approved": approved, "always": always,
	})
}

// requestModels asks the server for the vault contents. validate=true makes
// the server actually authenticate each key against its provider, which is a
// real network round-trip per model — noticeably slow, so it's opt-in.
func requestModels(conn *websocket.Conn, validate bool) error {
	return wsjson.Write(context.Background(), conn, map[string]any{
		"type": "list_models", "validate": validate,
	})
}

// setModelKey saves an API key for one model. The server writes it into .env
// and re-validates against the real provider, then replies with a fresh
// "models" payload — so the caller never needs a separate refresh.
//
// The key crosses the socket in plaintext. That's acceptable only because
// server.py binds localhost; it would not be if the backend ever moved off
// the machine (phase 7), which is where real key storage has to land.
func setModelKey(conn *websocket.Conn, modelKey, value string) error {
	return wsjson.Write(context.Background(), conn, map[string]any{
		"type": "set_model_key", "model_key": modelKey, "value": value,
	})
}

// sendHumanMessage writes a chat message out on the same connection listen()
// is separately reading — safe, since reading and writing are independent
// directions on one WebSocket connection.
func sendHumanMessage(conn *websocket.Conn, sessionID string, taskID *string, content string) error {
	return wsjson.Write(context.Background(), conn, map[string]any{
		"type": "human_message", "session_id": sessionID, "task_id": taskID, "content": content,
	})
}
