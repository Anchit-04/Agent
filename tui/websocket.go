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
	TaskID   *string `json:"task_id"`
	ModelKey string  `json:"model_key"`
	Text     string  `json:"text"`
}

type toolCallMsg struct {
	TaskID *string        `json:"task_id"`
	Name   string         `json:"name"`
	Args   map[string]any `json:"args"`
}

type toolResultMsg struct {
	TaskID  *string `json:"task_id"`
	Name    string  `json:"name"`
	Content string  `json:"content"`
}

type sessionEndedMsg struct {
	Status    string `json:"status"`
	FinalText string `json:"final_text"`
}

type serverErrorMsg struct {
	Message string `json:"message"`
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
		case "error":
			var m serverErrorMsg
			json.Unmarshal(data, &m)
			p.Send(m)
		}
	}
}

// sendHumanMessage writes a chat message out on the same connection listen()
// is separately reading — safe, since reading and writing are independent
// directions on one WebSocket connection.
func sendHumanMessage(conn *websocket.Conn, sessionID string, taskID *string, content string) error {
	return wsjson.Write(context.Background(), conn, map[string]any{
		"type": "human_message", "session_id": sessionID, "task_id": taskID, "content": content,
	})
}
