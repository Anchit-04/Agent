// tui is the terminal client — connects to loom/server.py over WebSocket.
// Chat defaults to the orchestrator's conversation; the card stack cycles
// discretely, not smoothly.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	"charm.land/bubbles/v2/key"
	"charm.land/bubbles/v2/textarea"
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
	// Which conversation this belongs to. The transcript is one list; the
	// viewport renders only the active thread's slice of it, so switching tabs
	// is a filter rather than a swap of buffers.
	threadID string
}

// A thread is a side conversation: its own session on the server, its own
// agent and history, seeded with the main thread's progress. Thread 0 is the
// session the TUI was launched with.
type thread struct {
	id      string // server session id; "" while the server hasn't replied yet
	label   string
	unread  bool
	pending bool // started, awaiting session_started
}

// labelFor turns a question into something that fits a tab.
func labelFor(question string) string {
	words := strings.Fields(strings.ToLower(question))
	keep := words
	if len(keep) > 3 {
		keep = keep[:3]
	}
	label := strings.Join(keep, "-")
	label = strings.TrimFunc(label, func(r rune) bool {
		return !(r >= 'a' && r <= 'z') && !(r >= '0' && r <= '9')
	})
	if label == "" {
		label = "thread"
	}
	return truncate(label, 16)
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
	foxRed   = lipgloss.Color("#e0362c") // the running fox itself — a true red, not
	//                                      the rust accent, which reads orange
	foxEmber = lipgloss.Color("#8a3b25") // idle borders, secondary accents
	foxInk   = lipgloss.Color("#ece0d4") // body text — warm off-white, not pure
	foxDim   = lipgloss.Color("#9a8579") // muted: placeholders, tool lines
)

var dimStyle = lipgloss.NewStyle().Foreground(foxDim)

type model struct {
	width, height int
	input         textarea.Model // textarea, not textinput: a single-line input
	//                              scrolls sideways past its width instead of
	//                              wrapping, so long messages were unreadable
	chat      viewport.Model
	cardIndex int
	quitting  bool

	conn         *websocket.Conn
	sessionID    string // the main session; threads[0].id mirrors it
	messages     []chatMsg
	disconnected bool

	threads  []thread // [0] is main; the rest are side conversations
	active   int      // index into threads — which tab is on screen
	modelKey string   // reused when opening a thread, so tabs match the main model

	working  bool // a turn is in flight — drives the fox animation
	foxFrame int

	panel         int // index into cards of the open panel, -1 = none
	models        []vaultEntry
	modelsLoading bool
	vaultRow      int             // selected model row in the Vault panel
	editingKey    bool            // typing a key into the selected row
	keyInput      textinput.Model // masked — never echo an API key

	// Pending approvals, oldest first. A queue rather than a single value
	// because concurrent executors can each park on their own request, and
	// dropping one would strand that thread until the session ends.
	approvals []approvalRequestedMsg

	// Which model the backend actually resolved to. Learned from the first
	// agent_turn rather than assumed from argv — omitting the model key makes
	// the server auto-pick, and silently running on a different model than you
	// intended is exactly how a provider outage gets misdiagnosed.
	activeModel string
}

// inputMinHeight/inputMaxHeight bound the input box: it starts one line tall
// and grows as you type, up to a point, after which the textarea scrolls
// internally rather than eating the chat.
const (
	inputMinHeight = 1
	inputMaxHeight = 6
)

func initialModel() model {
	ti := textarea.New()
	ti.Focus()
	ti.SetHeight(inputMinHeight)
	ti.Prompt = "" // the surrounding border is the affordance
	ti.ShowLineNumbers = false
	ti.Placeholder = "ask fox…"
	// Enter sends; the textarea's own newline binding is moved out of the way
	// so it can't swallow the send. alt+enter inserts a line break instead.
	ti.KeyMap.InsertNewline = key.NewBinding(
		key.WithKeys("alt+enter"),
		key.WithHelp("alt+enter", "newline"),
	)
	// Styles is a getter in v2 — mutate a copy, then SetStyles. Text colour is
	// set on both states so the box doesn't change shade when focus moves to a
	// panel, and CursorLine is cleared because its default highlight fights
	// the panel background behind it.
	st := ti.Styles()
	st.Focused.Text = lipgloss.NewStyle().Foreground(foxInk)
	st.Blurred.Text = lipgloss.NewStyle().Foreground(foxInk)
	st.Focused.Placeholder = dimStyle
	st.Blurred.Placeholder = dimStyle
	st.Focused.CursorLine = lipgloss.NewStyle()
	ti.SetStyles(st)

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

// inputTextWidth is the usable text column count inside the input box: what
// SetWidth was given, less one so the cursor sitting past the last character
// doesn't push a wrap a column early.
func (m model) inputTextWidth() int {
	w := m.width - 9
	if w < 1 {
		w = 1
	}
	return w
}

// displayRows counts the rows text occupies once wrapped at `width`.
// textarea.LineCount() is len(m.value) — *logical* lines, so a long paragraph
// with no newline in it returns 1 no matter how far it wraps. That's the
// number we actually need to size the box, so compute it here.
func displayRows(s string, width int) int {
	rows := 0
	for _, line := range strings.Split(s, "\n") {
		r := (lipgloss.Width(line) + width - 1) / width
		if r < 1 {
			r = 1 // an empty line still occupies a row
		}
		rows += r
	}
	return rows
}

// syncInputHeight grows the input box to fit what's been typed, within bounds,
// so a long message reads as a paragraph instead of scrolling sideways.
func (m *model) syncInputHeight() {
	h := displayRows(m.input.Value(), m.inputTextWidth())
	if h < inputMinHeight {
		h = inputMinHeight
	}
	if h > inputMaxHeight {
		h = inputMaxHeight
	}
	m.input.SetHeight(h)
	m.resizeChat(h)
}

// resizeChat keeps the transcript and the input adding up to the frame. Every
// line the input gains has to come out of the chat, or the layout overflows
// the terminal and the bottom of the frame is pushed off screen.
func (m *model) resizeChat(inputHeight int) {
	if m.width == 0 {
		return // no size yet; WindowSizeMsg will call us
	}
	cw := m.width - 28
	ch := m.height - 10 - (inputHeight - inputMinHeight) - m.threadBarRows()
	if cw < 1 {
		cw = 1
	}
	if ch < 1 {
		ch = 1
	}
	m.chat.SetWidth(cw)
	m.chat.SetHeight(ch)
	m.refreshChat() // block widths are baked in at render time — rewrap
}

// threadBarRows is 1 once a second thread exists, 0 before that — the bar is
// hidden for single-thread sessions, and the chat only pays for it when shown.
func (m model) threadBarRows() int {
	if len(m.threads) < 2 {
		return 0
	}
	return 1
}

// maxReplayEvents caps how much history a freshly attached window rebuilds.
// SessionState.turns grows without bound — every tool_call, tool_result and
// agent_turn is appended for the life of the session — so a long-running one
// would otherwise spend its first frame rendering megabytes.
const maxReplayEvents = 200

// replay rebuilds the transcript from a session's stored event log, using the
// same mapping listen() applies to live events. Kept deliberately in step with
// the Update cases below: if one learns to render a new event type, so should
// this, or attaching would show a different conversation than watching live.
func (m *model) replay(threadID string, events []storedEvent) {
	if len(events) > maxReplayEvents {
		m.pushTo(threadID, msgNotice,
			fmt.Sprintf("…%d earlier events not shown", len(events)-maxReplayEvents))
		events = events[len(events)-maxReplayEvents:]
	}
	str := func(p map[string]any, k string) string {
		if v, ok := p[k].(string); ok {
			return v
		}
		return ""
	}
	for _, ev := range events {
		switch ev.Type {
		case "agent_turn":
			if text := strings.TrimSpace(str(ev.Payload, "text")); text != "" {
				m.pushTo(threadID, msgAgent, text)
			}
		case "human_message_injected":
			if c := strings.TrimSpace(str(ev.Payload, "content")); c != "" {
				m.pushTo(threadID, msgUser, c)
			}
		case "tool_call":
			args, _ := ev.Payload["args"].(map[string]any)
			m.pushTo(threadID, msgTool, "→ "+str(ev.Payload, "name")+"("+formatToolArgs(args)+")")
		case "tool_result":
			m.pushTo(threadID, msgTool, "  "+summarizeToolResult(str(ev.Payload, "content")))
		case "tool_denied":
			m.pushTo(threadID, msgNotice, "denied: "+str(ev.Payload, "name"))
		case "error":
			m.pushTo(threadID, msgNotice, "error: "+str(ev.Payload, "message"))
		}
	}
}

// popOut opens this thread in its own terminal window, attached to the same
// session. Both windows stay live: the server broadcasts to every client in
// session.clients, so neither is a copy.
//
// There is no portable way to open a terminal. Windows gets a real
// implementation; everywhere else returns the command to run by hand, which is
// honest rather than pretending at support that doesn't exist yet.
func popOutCmd(sessionID string) tea.Cmd {
	return func() tea.Msg {
		exe, err := os.Executable()
		if err != nil {
			return serverErrorMsg{Message: "could not locate the fox binary: " + err.Error()}
		}
		if runtime.GOOS != "windows" {
			return popOutManualMsg{Command: fmt.Sprintf("%s --attach %s", exe, sessionID)}
		}
		// Windows Terminal if present, since it opens a tab in the existing
		// window; plain cmd otherwise.
		if wt, err := exec.LookPath("wt.exe"); err == nil {
			if err := exec.Command(wt, "new-tab", exe, "--attach", sessionID).Start(); err == nil {
				return popOutDoneMsg{}
			}
		}
		// The empty "" is the window title. start treats a leading quoted
		// argument as the title, so without it a quoted exe path is swallowed
		// as one and nothing launches.
		if err := exec.Command("cmd", "/c", "start", "", exe, "--attach", sessionID).Start(); err != nil {
			return popOutManualMsg{Command: fmt.Sprintf("%s --attach %s", exe, sessionID)}
		}
		return popOutDoneMsg{}
	}
}

type popOutDoneMsg struct{}

type popOutManualMsg struct{ Command string }

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

func sendApprovalCmd(conn *websocket.Conn, sessionID string, req approvalRequestedMsg,
	approved, always bool) tea.Cmd {
	return func() tea.Msg {
		if err := sendApprovalResponse(conn, sessionID, req.RequestID, req.ToolName, approved, always); err != nil {
			// The agent thread is still parked and now has no way to be
			// answered — say so rather than leaving a silent hang.
			return serverErrorMsg{Message: "approval reply failed, agent still waiting: " + err.Error()}
		}
		return nil
	}
}

func startThreadCmd(conn *websocket.Conn, question, parentID, modelKey string) tea.Cmd {
	return func() tea.Msg {
		if err := startThread(conn, question, parentID, modelKey); err != nil {
			return serverErrorMsg{Message: "could not open thread: " + err.Error()}
		}
		return nil
	}
}

// renderThreadBar shows the open conversations. Hidden when there's only one,
// so a session that never uses threads looks exactly as it did before.
func (m model) renderThreadBar(width int) string {
	if len(m.threads) < 2 {
		return ""
	}
	parts := make([]string, 0, len(m.threads))
	for i, th := range m.threads {
		label := fmt.Sprintf("%d %s", i+1, th.label)
		switch {
		case i == m.active:
			parts = append(parts, lipgloss.NewStyle().Foreground(foxRust).Bold(true).Render("["+label+"]"))
		case th.unread:
			parts = append(parts, lipgloss.NewStyle().Foreground(foxInk).Render(" "+label+" •"))
		default:
			parts = append(parts, dimStyle.Render(" "+label+"  "))
		}
	}
	return lipgloss.NewStyle().Width(width).PaddingLeft(2).
		Render(strings.Join(parts, " ") + dimStyle.Render("   ctrl+t new"))
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
	m.pushTo(m.activeThreadID(), kind, text)
}

// pushTo files a message under a specific thread. Events arrive tagged with a
// session id, so a background thread's output lands in its own tab instead of
// interleaving into whatever you happen to be reading.
func (m *model) pushTo(threadID string, kind msgKind, text string) {
	wasAtBottom := m.chat.AtBottom()
	m.messages = append(m.messages, chatMsg{kind: kind, text: text, threadID: threadID})

	if threadID != m.activeThreadID() {
		for i := range m.threads {
			if m.threads[i].id == threadID {
				m.threads[i].unread = true
			}
		}
		return // not on screen; nothing to re-render or scroll
	}
	m.refreshChat()
	if wasAtBottom {
		m.chat.GotoBottom()
	}
}

func (m model) activeThreadID() string {
	if m.active < len(m.threads) {
		return m.threads[m.active].id
	}
	return m.sessionID
}

// switchThread changes tabs. The viewport is rebuilt from the same message
// list, filtered — so history for every thread survives switching away.
func (m *model) switchThread(i int) {
	if i < 0 || i >= len(m.threads) || i == m.active {
		return
	}
	m.active = i
	m.threads[i].unread = false
	m.refreshChat()
	m.chat.GotoBottom()
}

// refreshChat re-renders every message at the current viewport width. Called
// on append and on resize, since block widths are baked in at render time.
func (m *model) refreshChat() {
	w := m.chat.Width()
	if w < 12 {
		w = 12
	}
	active := m.activeThreadID()
	blocks := make([]string, 0, len(m.messages))
	for _, msg := range m.messages {
		if msg.threadID != active {
			continue // belongs to another tab
		}
		blocks = append(blocks, renderMsg(msg, w))
	}
	if len(blocks) == 0 {
		m.chat.SetContent(dimStyle.Render("(waiting for the agent…)"))
		return
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
		run := lipgloss.NewStyle().Foreground(foxRed)
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
		// syncInputHeight -> resizeChat owns all chat sizing, so the input's
		// current height is always accounted for. Sizing it here as well would
		// fight that and leave the chat too tall whenever the input has grown.
		m.syncInputHeight()
		return m, nil

	case tea.MouseWheelMsg:
		// The wheel belongs to the transcript, full stop. Without mouse
		// tracking enabled (View.MouseMode below), terminals translate the
		// wheel into arrow-key sequences — which is why scrolling was
		// cycling the card stack instead of moving the chat.
		switch msg.Button {
		case tea.MouseWheelUp:
			m.chat.ScrollUp(3)
		case tea.MouseWheelDown:
			m.chat.ScrollDown(3)
		}
		return m, nil

	case foxTickMsg:
		if m.working {
			m.foxFrame++
		}
		return m, foxTick()

	case popOutDoneMsg:
		m.pushMessage(msgNotice, "opened this thread in a new window")
		return m, nil

	case popOutManualMsg:
		m.pushMessage(msgNotice, "run this in another terminal:")
		m.pushMessage(msgNotice, "  "+msg.Command)
		return m, nil

	case sessionStartedMsg:
		// A thread we opened now has its session. Messages we filed under the
		// placeholder ("" id) are re-tagged so the question you typed stays in
		// its tab rather than vanishing when the real id arrives.
		for i := range m.threads {
			if m.threads[i].pending {
				m.threads[i].id = msg.SessionID
				m.threads[i].pending = false
				for j := range m.messages {
					if m.messages[j].threadID == "" {
						m.messages[j].threadID = msg.SessionID
					}
				}
				break
			}
		}
		m.refreshChat()
		return m, nil

	case modelsMsg:
		m.modelsLoading = false
		m.models = msg.Models
		return m, nil

	case approvalRequestedMsg:
		// The fox stops running: the agent isn't working, it's waiting on you.
		m.working = false
		m.approvals = append(m.approvals, msg)
		return m, nil

	case tea.KeyPressMsg:
		key := msg.String()

		// An approval blocks an agent thread, so it takes the keyboard ahead
		// of everything — including an open panel. Any other key is ignored
		// rather than falling through, so a stray keystroke can't answer for
		// you or leak into the input box behind the overlay.
		if len(m.approvals) > 0 {
			req := m.approvals[0]
			var approved, always bool
			switch key {
			case "ctrl+c":
				m.quitting = true
				return m, tea.Quit
			case "y":
				approved = true
			case "a":
				approved, always = true, true
			case "n", "esc":
				// denial
			default:
				return m, nil
			}
			m.approvals = m.approvals[1:]
			verdict := "denied"
			if always {
				verdict = "allowed (always, this session)"
			} else if approved {
				verdict = "allowed"
			}
			m.pushMessage(msgNotice, verdict+": "+req.Description)
			return m, sendApprovalCmd(m.conn, m.sessionID, req, approved, always)
		}

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
		case "ctrl+t":
			// Same text, different destination: whatever you've typed becomes a
			// side conversation instead of going to the current thread. Avoids
			// a separate modal just to collect the question.
			question := strings.TrimSpace(m.input.Value())
			if question == "" || m.disconnected {
				return m, nil
			}
			m.input.Reset()
			m.syncInputHeight()
			m.threads = append(m.threads, thread{label: labelFor(question), pending: true})
			m.switchThread(len(m.threads) - 1)
			m.pushMessage(msgUser, question)
			m.working = true
			return m, startThreadCmd(m.conn, question, m.sessionID, m.modelKey)

		case "ctrl+o":
			// Mirror this thread into its own terminal. The tab stays: both
			// windows are live clients of the same session.
			target := m.activeThreadID()
			if target == "" {
				m.pushMessage(msgNotice, "this thread is still starting")
				return m, nil
			}
			return m, popOutCmd(target)

		case "ctrl+1", "ctrl+2", "ctrl+3", "ctrl+4", "ctrl+5",
			"ctrl+6", "ctrl+7", "ctrl+8", "ctrl+9":
			m.switchThread(int(key[len(key)-1] - '1'))
			return m, nil

		case "enter":
			// Send. The textarea's newline binding was moved to alt+enter in
			// initialModel, so enter can't be swallowed as a line break.
			content := strings.TrimSpace(m.input.Value())
			m.input.Reset()
			m.syncInputHeight() // shrink back to one line after sending
			if content == "" || m.disconnected {
				return m, nil
			}
			// Goes to the thread you're looking at, not always the main
			// session — otherwise replies in a side conversation would land
			// in the main task's agent.
			target := m.activeThreadID()
			if target == "" {
				m.pushMessage(msgNotice, "this thread is still starting — try again in a moment")
				return m, nil
			}
			m.pushMessage(msgUser, content)
			m.working = true // fox starts running until the reply lands
			if err := sendHumanMessage(m.conn, target, nil, content); err != nil {
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
			m.pushTo(msg.SessionID, msgAgent, text)
		}
		return m, nil

	case toolCallMsg:
		m.working = true // still mid-turn even though fox already spoke
		m.pushTo(msg.SessionID, msgTool, "→ "+msg.Name+"("+formatToolArgs(msg.Args)+")")
		return m, nil

	case toolResultMsg:
		m.pushTo(msg.SessionID, msgTool, "  "+summarizeToolResult(msg.Content))
		return m, nil

	case sessionEndedMsg:
		m.working = false
		if msg.Status == "failed" {
			m.pushTo(msg.SessionID, msgNotice, "session failed: "+msg.FinalText)
		} else {
			m.pushTo(msg.SessionID, msgNotice, "session ended")
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
	m.syncInputHeight() // the box tracks what you've typed, so wrapping is visible
	return m, cmd
}

func (m model) View() tea.View {
	if m.quitting {
		return tea.NewView("")
	}
	if m.width == 0 {
		v := tea.NewView(lipgloss.NewStyle().Foreground(foxDim).Render("initializing…"))
		v.AltScreen = true
		v.MouseMode = tea.MouseModeCellMotion
		v.BackgroundColor = foxBG
		return v
	}

	// Mirrors resizeChat: every line the input grew costs the chat one.
	chatH := m.height - 8 - (m.input.Height() - inputMinHeight) - m.threadBarRows()
	if chatH < 1 {
		chatH = 1
	}
	chat := lipgloss.NewStyle().
		Width(m.width-24).
		Height(chatH).
		Padding(1, 2).
		Foreground(foxInk).
		Render(m.chat.View())

	top := lipgloss.JoinHorizontal(lipgloss.Top, chat, renderCardStack(m.cardIndex, 18))
	if m.panel >= 0 {
		top = m.renderPanel(m.width-2, m.height-6)
	}
	// Outranks the panel: an approval is holding an agent thread open.
	if len(m.approvals) > 0 {
		top = m.renderApproval(m.width-2, m.height-6)
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

	parts := []string{top, fox}
	if bar := m.renderThreadBar(m.width - 4); bar != "" {
		parts = append(parts, bar)
	}
	body := lipgloss.JoinVertical(lipgloss.Left, append(parts, input)...)

	outer := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(foxEmber).
		Width(m.width - 2).
		Height(m.height - 2).
		Render(body)

	v := tea.NewView(outer)
	v.AltScreen = true
	// Enables real wheel events; without it the terminal sends arrow keys
	// for the wheel and scrolling hits the card stack instead of the chat.
	v.MouseMode = tea.MouseModeCellMotion
	v.BackgroundColor = foxBG
	return v
}

// renderApproval draws the pending approval over the chat area. Deliberately
// states *why* it's asking — policy only escalates on boundary crossings, so
// the reason is the useful part, not the tool name.
func (m model) renderApproval(width, height int) string {
	req := m.approvals[0]
	inner := width - 8
	if inner < 20 {
		inner = 20
	}
	wrap := lipgloss.NewStyle().Width(inner)

	title := lipgloss.NewStyle().Foreground(foxRust).Bold(true).Render("fox wants to")
	what := wrap.Foreground(foxInk).Render(req.Description)
	why := wrap.Foreground(foxDim).Render("why ask: " + req.Reason)

	keys := lipgloss.NewStyle().Foreground(foxInk).Render(
		"[y] allow   [n] deny   [a] always allow " + req.ToolName)
	queued := ""
	if len(m.approvals) > 1 {
		queued = dimStyle.Render(fmt.Sprintf("\n%d more waiting", len(m.approvals)-1))
	}

	body := title + "\n\n" + what + "\n" + why + "\n\n" + keys + queued
	box := lipgloss.NewStyle().
		Width(inner+2).
		Padding(1, 2).
		Border(lipgloss.DoubleBorder()).
		BorderForeground(foxRust).
		Render(body)

	return lipgloss.NewStyle().
		Width(width).
		Height(height).
		Align(lipgloss.Center, lipgloss.Center).
		Render(box)
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
	const url = "ws://localhost:8765"

	// --attach joins a session that already exists instead of starting one.
	// That's what a popped-out thread runs, and it's also how you get back to
	// a session whose window you closed — the session keeps running on the
	// server either way.
	if len(os.Args) >= 3 && os.Args[1] == "--attach" {
		conn, reply, err := attach(url, os.Args[2])
		if err != nil {
			fmt.Fprintln(os.Stderr, "failed to attach:", err)
			os.Exit(1)
		}
		m := initialModel()
		m.conn = conn
		m.sessionID = reply.SessionID
		m.activeModel = reply.State.ModelKey
		m.threads = []thread{{id: reply.SessionID, label: "attached"}}
		m.replay(reply.SessionID, reply.State.Turns)

		p := tea.NewProgram(m)
		go listen(conn, p)
		if _, err := p.Run(); err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			os.Exit(1)
		}
		return
	}

	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, `Usage: tui "<task>" [model_key]`)
		fmt.Fprintln(os.Stderr, `       tui --attach <session_id>`)
		os.Exit(1)
	}
	task := os.Args[1]
	modelKey := "" // empty means "let the backend pick any available key"
	if len(os.Args) > 2 {
		modelKey = os.Args[2]
	}

	conn, sessionID, err := connect(url, task, modelKey, "agent")
	if err != nil {
		fmt.Fprintln(os.Stderr, "failed to connect:", err)
		os.Exit(1)
	}

	m := initialModel()
	m.conn = conn
	m.sessionID = sessionID
	m.modelKey = modelKey
	// Thread 0 is the session we just started. Everything else in the tab bar
	// is a side conversation opened later with ctrl+t.
	m.threads = []thread{{id: sessionID, label: "main"}}

	p := tea.NewProgram(m)
	go listen(conn, p)

	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
