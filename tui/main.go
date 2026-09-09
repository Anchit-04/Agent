// tui is the terminal client — connects to loom/server.py over WebSocket.
// Chat defaults to the orchestrator's conversation; the card stack cycles
// discretely, not smoothly.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"charm.land/bubbles/v2/textinput"
	"charm.land/bubbles/v2/viewport"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/coder/websocket"
)

type card struct {
	name string
}

// Messages are kept as structs rather than pre-rendered strings so the whole
// transcript can be re-laid-out on resize — the old []string couldn't rewrap.
type msgKind int

const (
	msgUser msgKind = iota
	msgAgent
	msgTool
	msgNotice
)

type chatMsg struct {
	kind msgKind
	text string
}

// Two-row pixel fox. The leg row alternates while the sprite also advances
// horizontally — at ~6fps the combination reads as running rather than
// twitching. Shown only while a turn is in flight.
var foxSprite = [][2]string{
	{"▗▛▀▜▄▖", "▝▘  ▝▘"},
	{"▗▛▀▜▄▖", "▘▝   ▘"},
	{"▗▛▀▜▄▖", "▝▘  ▘▝"},
	{"▗▛▀▜▄▖", " ▘▝ ▝▘"},
}

type foxTickMsg time.Time

func foxTick() tea.Cmd {
	return tea.Tick(time.Second/6, func(t time.Time) tea.Msg { return foxTickMsg(t) })
}

// Snapshot was removed 2026-09-08 — it isn't a card. The intended flow is
// selecting a line or paragraph of text and turning it into a post-it note,
// which is a text-selection interaction, not a panel. See PLAN.md item 4.
var cards = []card{
	{"Personas"},
	{"Vault"},
}

// Fox's warm-dark palette. foxBG is a near-black with a heavy red bias —
// it reads as "Fox has an identity" without the contrast cost of an actual
// brown behind body text. Color is spent on foxRust (the accent) instead.
var (
	foxBG    = lipgloss.Color("#211310") // terminal background (tea.View.BackgroundColor)
	foxPanel = lipgloss.Color("#2c1a16") // input bar fill — one step up for depth
	foxRust  = lipgloss.Color("#c8623a") // active borders, focused card, the pop
	foxEmber = lipgloss.Color("#8a3b25") // idle borders, secondary accents
	foxInk   = lipgloss.Color("#ece0d4") // body text — warm off-white, not pure
	foxDim   = lipgloss.Color("#9a8579") // muted: placeholders, tool lines
)

var dimStyle = lipgloss.NewStyle().Foreground(foxDim)

type model struct {
	width, height int
	input         textinput.Model
	chat          viewport.Model
	cardIndex     int
	quitting      bool

	conn         *websocket.Conn
	sessionID    string
	messages     []chatMsg
	disconnected bool

	working  bool // a turn is in flight — drives the fox animation
	foxFrame int

	panel         int // index into cards of the open panel, -1 = none
	models        []vaultEntry
	modelsLoading bool
	vaultRow      int             // selected model row in the Vault panel
	editingKey    bool            // typing a key into the selected row
	keyInput      textinput.Model // masked — never echo an API key

	// Which model the backend actually resolved to. Learned from the first
	// agent_turn rather than assumed from argv — omitting the model key makes
	// the server auto-pick, and silently running on a different model than you
	// intended is exactly how a provider outage gets misdiagnosed.
	activeModel string
}

func initialModel() model {
	ti := textinput.New()
	ti.Focus()
	ti.SetWidth(60)

	vp := viewport.New()
	vp.SoftWrap = true // wrap long lines instead of forcing horizontal scroll
	vp.Style = lipgloss.NewStyle().Foreground(foxInk)
	vp.SetContent(lipgloss.NewStyle().Foreground(foxDim).Render("(waiting for the agent…)"))

	ki := textinput.New()
	ki.EchoMode = textinput.EchoPassword
	ki.SetWidth(48)
	ki.Placeholder = "paste key, enter to save"

	return model{input: ti, chat: vp, panel: -1, keyInput: ki}
}

// openPanel focuses a card as a full panel. Vault pulls fresh data on open
// rather than caching — keys can be added to .env while the TUI is running.
func (m *model) openPanel(i int) tea.Cmd {
	m.panel = i
	if cards[i].name == "Vault" {
		m.modelsLoading = true
		return requestModelsCmd(m.conn, false)
	}
	return nil
}

func requestModelsCmd(conn *websocket.Conn, validate bool) tea.Cmd {
	return func() tea.Msg {
		if err := requestModels(conn, validate); err != nil {
			return serverErrorMsg{Message: "vault request failed: " + err.Error()}
		}
		return nil
	}
}

func setModelKeyCmd(conn *websocket.Conn, modelKey, value string) tea.Cmd {
	return func() tea.Msg {
		if err := setModelKey(conn, modelKey, value); err != nil {
			return serverErrorMsg{Message: "saving key failed: " + err.Error()}
		}
		return nil
	}
}

// pushMessage appends to the transcript and refreshes the chat viewport. It
// only auto-scrolls to the bottom if you were already there — so scrolling up
// to read history isn't yanked back down by new events.
func (m *model) pushMessage(kind msgKind, text string) {
	wasAtBottom := m.chat.AtBottom()
	m.messages = append(m.messages, chatMsg{kind: kind, text: text})
	m.refreshChat()
	if wasAtBottom {
		m.chat.GotoBottom()
	}
}

// refreshChat re-renders every message at the current viewport width. Called
// on append and on resize, since block widths are baked in at render time.
func (m *model) refreshChat() {
	w := m.chat.Width()
	if w < 12 {
		w = 12
	}
	if len(m.messages) == 0 {
		m.chat.SetContent(dimStyle.Render("(waiting for the agent…)"))
		return
	}
	blocks := make([]string, 0, len(m.messages))
	for _, msg := range m.messages {
		blocks = append(blocks, renderMsg(msg, w))
	}
	m.chat.SetContent(strings.Join(blocks, "\n\n"))
}

// renderMsg gives each speaker its own shape: your messages get a rust rule
// down the left edge, Fox's are flush, tool traffic is indented and dimmed.
// The label sits on its own line so wrapped body text aligns with itself
// instead of hanging off a "you: " prefix.
func renderMsg(msg chatMsg, width int) string {
	body := lipgloss.NewStyle().Foreground(foxInk).Width(width - 3)

	switch msg.kind {
	case msgUser:
		label := lipgloss.NewStyle().Foreground(foxRust).Bold(true).Render("you")
		return lipgloss.NewStyle().
			Border(lipgloss.ThickBorder(), false, false, false, true).
			BorderForeground(foxRust).
			PaddingLeft(1).
			Render(label + "\n" + body.Render(msg.text))

	case msgAgent:
		label := lipgloss.NewStyle().Foreground(foxEmber).Bold(true).Render("fox")
		return label + "\n" + body.Render(msg.text)

	case msgTool:
		return dimStyle.Width(width - 2).PaddingLeft(2).Render(msg.text)

	default:
		return lipgloss.NewStyle().Foreground(foxEmber).Italic(true).
			Width(width - 1).Render(msg.text)
	}
}

// renderFox is a fixed-height strip so the layout never jumps between the
// idle and running states.
func (m model) renderFox(width int) string {
	label := m.activeModel
	if label == "" {
		label = "connecting…"
	}
	tag := dimStyle.Render(label)
	tagW := lipgloss.Width(label) + 2

	var left string
	if !m.working {
		left = dimStyle.Render("ready") + "\n"
	} else {
		f := foxSprite[m.foxFrame%len(foxSprite)]
		track := width - tagW - 10
		if track < 1 {
			track = 1
		}
		pad := strings.Repeat(" ", (m.foxFrame/2)%track)
		run := lipgloss.NewStyle().Foreground(foxRust)
		left = run.Render(pad+f[0]) + "\n" + run.Render(pad+f[1])
	}

	// Model tag is pinned right so it stays put while the fox runs beneath it.
	return lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.NewStyle().Width(width-tagW).Render(left), tag)
}

func (m model) Init() tea.Cmd {
	return foxTick()
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.input.SetWidth(m.width - 8)
		// chat box is Width(m.width-24) Height(m.height-6) with Padding(1,2),
		// so the inner content area is 4 narrower and 2 shorter.
		// -10 rather than -8: the fox strip below the chat is two rows tall.
		cw, ch := m.width-28, m.height-10
		if cw < 1 {
			cw = 1
		}
		if ch < 1 {
			ch = 1
		}
		m.chat.SetWidth(cw)
		m.chat.SetHeight(ch)
		m.refreshChat() // block widths are baked in at render time — rewrap them
		return m, nil

	case foxTickMsg:
		if m.working {
			m.foxFrame++
		}
		return m, foxTick()

	case modelsMsg:
		m.modelsLoading = false
		m.models = msg.Models
		return m, nil

	case tea.KeyPressMsg:
		key := msg.String()

		// An open panel owns the keyboard. Without this, keystrokes would
		// fall through to m.input.Update below and land invisibly in the
		// input box hidden behind the panel.
		if m.panel >= 0 {
			// Key entry swallows everything except save/cancel, so a pasted
			// key containing "v" or "tab" can't trigger a panel action.
			if m.editingKey {
				switch key {
				case "esc":
					m.editingKey = false
					m.keyInput.Reset()
					return m, nil
				case "enter":
					val := strings.TrimSpace(m.keyInput.Value())
					m.editingKey = false
					m.keyInput.Reset()
					if val == "" || m.vaultRow >= len(m.models) {
						return m, nil
					}
					m.modelsLoading = true
					return m, setModelKeyCmd(m.conn, m.models[m.vaultRow].ModelKey, val)
				}
				var cmd tea.Cmd
				m.keyInput, cmd = m.keyInput.Update(msg)
				return m, cmd
			}

			switch key {
			case "ctrl+c":
				m.quitting = true
				return m, tea.Quit
			case "esc":
				m.panel = -1
			case "tab":
				// Cycle panels rather than close — ↑/↓ now belong to row
				// selection inside the panel.
				m.cardIndex = (m.cardIndex + 1) % len(cards)
				return m, m.openPanel(m.cardIndex)
			case "up":
				if m.vaultRow > 0 {
					m.vaultRow--
				}
			case "down":
				if m.vaultRow < len(m.models)-1 {
					m.vaultRow++
				}
			case "enter":
				if cards[m.panel].name == "Vault" && m.vaultRow < len(m.models) {
					m.editingKey = true
					m.keyInput.Focus()
				}
			case "v":
				// Real auth check against every provider — seconds, not ms.
				if cards[m.panel].name == "Vault" {
					m.modelsLoading = true
					return m, requestModelsCmd(m.conn, true)
				}
			}
			return m, nil
		}

		switch key {
		case "ctrl+c", "esc":
			m.quitting = true
			return m, tea.Quit
		case "tab":
			return m, m.openPanel(m.cardIndex)
		case "up":
			m.cardIndex = (m.cardIndex - 1 + len(cards)) % len(cards)
			return m, nil
		case "down":
			m.cardIndex = (m.cardIndex + 1) % len(cards)
			return m, nil
		case "pgup":
			m.chat.PageUp()
			return m, nil
		case "pgdown":
			m.chat.PageDown()
			return m, nil
		case "shift+up":
			m.chat.ScrollUp(1)
			return m, nil
		case "shift+down":
			m.chat.ScrollDown(1)
			return m, nil
		case "enter":
			content := strings.TrimSpace(m.input.Value())
			m.input.Reset()
			if content == "" || m.disconnected {
				return m, nil
			}
			m.pushMessage(msgUser, content)
			m.working = true // fox starts running until the reply lands
			if err := sendHumanMessage(m.conn, m.sessionID, nil, content); err != nil {
				m.working = false
				m.pushMessage(msgNotice, "failed to send: "+err.Error())
			}
			return m, nil
		}

	case agentTurnMsg:
		// Captured before the empty-text check — a turn that's only tool calls
		// still tells us which model resolved, and that's the first event we
		// get, so the status strip fills in immediately.
		m.activeModel = msg.ModelKey
		// Labelled "fox", not msg.ModelKey — the model is an implementation
		// detail the agent discloses on request, not a speaker name. Some
		// models also return empty text alongside their tool calls; rendering
		// that as a bare "fox" label just adds noise between the tool lines.
		// Trimmed, not just tested — models routinely lead with blank lines,
		// which rendered as a gap between the "fox" label and its own text.
		if text := strings.TrimSpace(msg.Text); text != "" {
			m.working = false
			m.pushMessage(msgAgent, text)
		}
		return m, nil

	case toolCallMsg:
		m.working = true // still mid-turn even though fox already spoke
		m.pushMessage(msgTool, "→ "+msg.Name+"("+formatToolArgs(msg.Args)+")")
		return m, nil

	case toolResultMsg:
		m.pushMessage(msgTool, "  "+summarizeToolResult(msg.Content))
		return m, nil

	case sessionEndedMsg:
		m.working = false
		if msg.Status == "failed" {
			m.pushMessage(msgNotice, "session failed: "+msg.FinalText)
		} else {
			m.pushMessage(msgNotice, "session ended")
		}
		return m, nil

	case serverErrorMsg:
		m.working = false
		m.pushMessage(msgNotice, "error: "+msg.Message)
		return m, nil

	case connectionLostMsg:
		m.disconnected = true
		m.working = false
		m.pushMessage(msgNotice, "disconnected: "+msg.Err.Error())
		return m, nil
	}

	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	return m, cmd
}

func (m model) View() tea.View {
	if m.quitting {
		return tea.NewView("")
	}
	if m.width == 0 {
		v := tea.NewView(lipgloss.NewStyle().Foreground(foxDim).Render("initializing…"))
		v.AltScreen = true
		v.BackgroundColor = foxBG
		return v
	}

	chat := lipgloss.NewStyle().
		Width(m.width-24).
		Height(m.height-8).
		Padding(1, 2).
		Foreground(foxInk).
		Render(m.chat.View())

	top := lipgloss.JoinHorizontal(lipgloss.Top, chat, renderCardStack(m.cardIndex, 18))
	if m.panel >= 0 {
		top = m.renderPanel(m.width-2, m.height-6)
	}

	fox := lipgloss.NewStyle().
		Width(m.width - 4).
		Height(2).
		PaddingLeft(2).
		Render(m.renderFox(m.width - 4))

	input := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(foxRust).
		Background(foxPanel).
		Foreground(foxInk).
		Width(m.width-4).
		Padding(0, 1).
		Render(m.input.View())

	body := lipgloss.JoinVertical(lipgloss.Left, top, fox, input)

	outer := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(foxEmber).
		Width(m.width - 2).
		Height(m.height - 2).
		Render(body)

	v := tea.NewView(outer)
	v.AltScreen = true
	v.BackgroundColor = foxBG
	return v
}

// renderPanel draws the open card as a full-width panel over the chat area.
func (m model) renderPanel(width, height int) string {
	title := lipgloss.NewStyle().Foreground(foxRust).Bold(true).
		Render(cards[m.panel].name)

	var hint, content string
	switch cards[m.panel].name {
	case "Vault":
		hint = "[↑↓] select  [enter] set key  [v] validate  [tab] next  [esc] close"
		if m.editingKey {
			hint = "[enter] save & validate   [esc] cancel"
		}
		content = m.renderVault()
	default:
		hint = "[tab] next panel   [esc] close"
		content = dimStyle.Render("Not built yet — see PLAN.md.")
	}

	head := lipgloss.JoinHorizontal(lipgloss.Top, title,
		lipgloss.NewStyle().Width(width-lipgloss.Width(title)-4).
			Align(lipgloss.Right).Foreground(foxDim).Render(hint))

	return lipgloss.NewStyle().
		Width(width-2).
		Height(height-2).
		Padding(1, 2).
		Border(lipgloss.RoundedBorder()).
		BorderForeground(foxRust).
		Render(head + "\n\n" + content)
}

// renderVault lists every registered model with its key status. `present`
// is a cheap env-var check; `validated` is a real auth round-trip and stays
// "unknown" until you press v — that distinction is the whole point of the
// panel, since a present-but-dead key is exactly what fails mid-run.
func (m model) renderVault() string {
	if m.modelsLoading && len(m.models) == 0 {
		return dimStyle.Render("loading…")
	}
	if len(m.models) == 0 {
		return dimStyle.Render("no models registered")
	}

	col := func(s string, w int) string {
		return lipgloss.NewStyle().Width(w).Render(s)
	}
	rows := []string{lipgloss.NewStyle().Foreground(foxDim).Bold(true).Render(
		col("  MODEL", 24) + col("TIER", 10) + col("KEY", 12) + "STATUS")}

	for i, e := range m.models {
		keyCell := dimStyle.Render("missing")
		if e.Present {
			keyCell = lipgloss.NewStyle().Foreground(foxInk).Render("present")
		}
		status := dimStyle.Render("unknown")
		if e.Validated != nil {
			if *e.Validated {
				status = lipgloss.NewStyle().Foreground(foxRust).Render("✓ valid")
			} else {
				status = lipgloss.NewStyle().Foreground(foxEmber).Render("✗ failed")
			}
		}
		name := lipgloss.NewStyle().Foreground(foxInk).Render(e.ModelKey)
		if !e.Present {
			name = dimStyle.Render(e.ModelKey)
		}

		cursor := "  "
		if i == m.vaultRow {
			cursor = lipgloss.NewStyle().Foreground(foxRust).Bold(true).Render("▸ ")
		}
		rows = append(rows, cursor+col(name, 22)+col(dimStyle.Render(e.Tier), 10)+
			col(keyCell, 12)+status)

		// The entry field opens inline under the selected row, so it's
		// obvious which model the key is being saved against.
		if i == m.vaultRow && m.editingKey {
			rows = append(rows, lipgloss.NewStyle().PaddingLeft(4).Render(
				lipgloss.NewStyle().Foreground(foxRust).Render("key ▸ ")+
					m.keyInput.View()))
		}
	}

	if m.modelsLoading {
		rows = append(rows, "", dimStyle.Render("checking with provider…"))
	}
	return strings.Join(rows, "\n")
}

// renderCardStack approximates overlap via left-indent + dimming on cards
// further from the focused one — a terminal can't truly overlap two cells.
func renderCardStack(focused, width int) string {
	rendered := make([]string, len(cards))
	for i, c := range cards {
		depth := abs(i - focused)
		style := lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			Width(width).
			Padding(0, 1).
			MarginLeft(depth)
		if i == focused {
			style = style.Bold(true).Foreground(foxInk).BorderForeground(foxRust)
		} else {
			style = style.Faint(true).Foreground(foxDim).BorderForeground(foxEmber)
		}
		rendered[i] = style.Render(c.name)
	}
	return lipgloss.JoinVertical(lipgloss.Left, rendered...)
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}

// truncate counts runes, not bytes — slicing a UTF-8 string at a byte offset
// can land mid-codepoint and emit a replacement char.
func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + fmt.Sprintf("… [%d chars]", len(r))
}

// oneLine flattens a payload to a single readable line. Tool results are JSON
// whose string fields carry real newlines and tabs; printed raw they dump
// literal \n and \t across the transcript.
func oneLine(s string) string {
	return strings.Join(strings.Fields(s), " ")
}

func numText(v any) string {
	if f, ok := v.(float64); ok { // all JSON numbers decode as float64
		return strconv.FormatFloat(f, 'f', -1, 64)
	}
	return fmt.Sprint(v)
}

// formatToolArgs renders a call's arguments compactly. Go's %v on a map prints
// unordered map[k:v] syntax, so keys are sorted and values rendered bare — a
// path reads better as read_file(loom/agent.py) than map[path:loom/agent.py].
func formatToolArgs(args map[string]any) string {
	if len(args) == 0 {
		return ""
	}
	keys := make([]string, 0, len(args))
	for k := range args {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	// A lone argument needs no name to be unambiguous.
	if len(keys) == 1 {
		return truncate(oneLine(fmt.Sprint(args[keys[0]])), 70)
	}
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+truncate(oneLine(fmt.Sprint(args[k])), 40))
	}
	return strings.Join(parts, ", ")
}

// summarizeToolResult reduces a tool's JSON payload to one human line. The
// shapes come from loom/tools/file_tools.py and sandbox/sandbox_client.py;
// anything unrecognised falls back to a flattened preview rather than spilling
// raw escapes into the chat.
func summarizeToolResult(content string) string {
	var obj map[string]any
	if json.Unmarshal([]byte(content), &obj) != nil {
		return truncate(oneLine(content), 140)
	}

	switch {
	case obj["error"] != nil:
		return "error: " + truncate(oneLine(fmt.Sprint(obj["error"])), 140)
	case obj["total_lines"] != nil: // read_file
		return numText(obj["total_lines"]) + " lines"
	case obj["count"] != nil: // list_directory
		return numText(obj["count"]) + " entries"
	case obj["bytes_written"] != nil: // write_file
		return "wrote " + numText(obj["bytes_written"]) + " bytes"
	case obj["exit_code"] != nil: // run_bash_command
		out := truncate(oneLine(fmt.Sprint(obj["output"])), 120)
		if numText(obj["exit_code"]) != "0" {
			return "exit " + numText(obj["exit_code"]) + ": " + out
		}
		if out == "" {
			return "ok"
		}
		return out
	case obj["success"] != nil: // edit_file
		if note, ok := obj["note"].(string); ok {
			return oneLine(note)
		}
		return "ok"
	}
	return truncate(oneLine(content), 140)
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, `Usage: tui "<task>" [model_key]`)
		os.Exit(1)
	}
	task := os.Args[1]
	modelKey := "" // empty means "let the backend pick any available key"
	if len(os.Args) > 2 {
		modelKey = os.Args[2]
	}

	conn, sessionID, err := connect("ws://localhost:8765", task, modelKey, "agent")
	if err != nil {
		fmt.Fprintln(os.Stderr, "failed to connect:", err)
		os.Exit(1)
	}

	m := initialModel()
	m.conn = conn
	m.sessionID = sessionID

	p := tea.NewProgram(m)
	go listen(conn, p)

	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
