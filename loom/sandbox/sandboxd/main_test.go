package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Verifies the whole process tree (not just cmd.exe itself) is really
// killed on timeout, not just detached while it keeps running.
func TestRunContained_TimeoutKillsProcessTree(t *testing.T) {
	dir := t.TempDir()
	counter := filepath.Join(dir, "counter.txt")

	// Appends to counter.txt once a second for up to 20s if left unkilled —
	// far longer than our 2s timeout below.
	cmd := `for /L %i in (1,1,20) do (echo %i>>` + counter + ` & ping -n 2 127.0.0.1 >nul)`

	start := time.Now()
	resp := runContained(cmd, 2*time.Second, 512)
	elapsed := time.Since(start)

	if resp.ExitCode != -1 {
		t.Errorf("expected exit_code -1 on timeout, got %d (output: %q)", resp.ExitCode, resp.Output)
	}
	if elapsed > 5*time.Second {
		t.Errorf("runContained took %v, expected it to return promptly after the 2s timeout", elapsed)
	}

	sizeAtTimeout, err := fileSize(counter)
	if err != nil {
		t.Fatalf("counter file should exist after the loop ran at least once: %v", err)
	}

	// If the tree survived, it'd write another line within the next ~2s.
	time.Sleep(3 * time.Second)
	sizeAfterWait, err := fileSize(counter)
	if err != nil {
		t.Fatalf("counter file vanished: %v", err)
	}
	if sizeAfterWait != sizeAtTimeout {
		t.Errorf("counter file grew after timeout (from %d to %d bytes) — process tree was not actually killed", sizeAtTimeout, sizeAfterWait)
	}
}

func fileSize(path string) (int64, error) {
	info, err := os.Stat(path)
	if err != nil {
		return 0, err
	}
	return info.Size(), nil
}
