// tui is the terminal client — connects to loom/server.py over WebSocket
// (not wired up yet, this is the rendering scaffold). Chat defaults to the
// orchestrator's conversation; the card stack cycles discretely, not smoothly.
package main

import (
	"fmt"
	"os"

	"charm.land/bubbles/v2/textinput"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
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
	cardIndex     int
	quitting      bool
}

func initialModel() model {
	ti := textinput.New()
	ti.Focus()
	ti.SetWidth(60)
	return model{input: ti}
}

func (m model) Init() tea.Cmd {
	return nil
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.input.SetWidth(m.width - 8)
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
		case "enter":
			m.input.Reset() // command parsing lands later — not designed yet
			return m, nil
		}
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
		Render("Chat window\n\n(orchestrator conversation streams here)")

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

func main() {
	p := tea.NewProgram(initialModel())
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
