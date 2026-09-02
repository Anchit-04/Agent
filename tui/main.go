// tui is the terminal client — connects to loom/server.py over WebSocket.
// Chat defaults to the orchestrator's conversation; the card stack cycles
// discretely, not smoothly.
package main

import (
	"fmt"
	"os"
	"strings"

	"charm.land/bubbles/v2/textinput"
	"charm.land/bubbles/v2/viewport"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/coder/websocket"
)

type card struct {
	name string
}

var cards = []card{
	{"Snapshot taker"},
	{"Personas"},
	{"Vault"},
}

type model struct {
	width, height int
	input         textinput.Model
	chat          viewport.Model
	cardIndex     int
	quitting      bool

	conn         *websocket.Conn
	sessionID    string
	messages     []string
	disconnected bool
}

func initialModel() model {
	ti := textinput.New()
	ti.Focus()
	ti.SetWidth(60)

	vp := viewport.New()
	vp.SoftWrap = true // wrap long lines instead of forcing horizontal scroll
	vp.SetContent("(waiting for the agent…)")

	return model{input: ti, chat: vp}
}

// pushMessage appends a line to the transcript and refreshes the chat
// viewport. It only auto-scrolls to the bottom if you were already there —
// so scrolling up to read history isn't yanked back down by new events.
func (m *model) pushMessage(line string) {
	wasAtBottom := m.chat.AtBottom()
	m.messages = append(m.messages, line)
	m.chat.SetContent(strings.Join(m.messages, "\n\n"))
	if wasAtBottom {
		m.chat.GotoBottom()
	}
}

func (m model) Init() tea.Cmd {
	return nil
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.input.SetWidth(m.width - 8)
		// chat box is Width(m.width-24) Height(m.height-6) with Padding(1,2),
		// so the inner content area is 4 narrower and 2 shorter.
		cw, ch := m.width-28, m.height-8
		if cw < 1 {
			cw = 1
		}
		if ch < 1 {
			ch = 1
		}
		m.chat.SetWidth(cw)
		m.chat.SetHeight(ch)
		return m, nil

	case tea.KeyPressMsg:
		switch msg.String() {
		case "ctrl+c", "esc":
			m.quitting = true
			return m, tea.Quit
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
			m.pushMessage("you: " + content)
			if err := sendHumanMessage(m.conn, m.sessionID, nil, content); err != nil {
				m.pushMessage("[failed to send]: " + err.Error())
			}
			return m, nil
		}

	case agentTurnMsg:
		m.pushMessage(fmt.Sprintf("%s: %s", msg.ModelKey, msg.Text))
		return m, nil

	case toolCallMsg:
		m.pushMessage(fmt.Sprintf("→ %s(%v)", msg.Name, msg.Args))
		return m, nil

	case toolResultMsg:
		m.pushMessage("  " + truncate(msg.Content, 300))
		return m, nil

	case sessionEndedMsg:
		if msg.Status == "failed" {
			m.pushMessage("[session failed]: " + msg.FinalText)
		} else {
			m.pushMessage("[session ended]")
		}
		return m, nil

	case serverErrorMsg:
		m.pushMessage("[error]: " + msg.Message)
		return m, nil

	case connectionLostMsg:
		m.disconnected = true
		m.pushMessage("[disconnected]: " + msg.Err.Error())
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
		v := tea.NewView("initializing...")
		v.AltScreen = true
		return v
	}

	chat := lipgloss.NewStyle().
		Width(m.width-24).
		Height(m.height-6).
		Padding(1, 2).
		Render(m.chat.View())

	top := lipgloss.JoinHorizontal(lipgloss.Top, chat, renderCardStack(m.cardIndex, 18))

	input := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		Width(m.width-4).
		Padding(0, 1).
		Render(m.input.View())

	body := lipgloss.JoinVertical(lipgloss.Left, top, input)

	outer := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		Width(m.width-2).
		Height(m.height-2).
		Render(body)

	v := tea.NewView(outer)
	v.AltScreen = true
	return v
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
			style = style.Bold(true).BorderForeground(lipgloss.Color("212"))
		} else {
			style = style.Faint(true)
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

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + fmt.Sprintf("... [%d chars total]", len(s))
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
