package main

import (
	"strings"
	"testing"
	"time"
)

// Verifies JOB_OBJECT_LIMIT_JOB_MEMORY is actually enforced, not just
// configured — a process committing ~700MB under a 100MB limit should fail.
func TestRunContained_MemoryLimitKillsProcess(t *testing.T) {
	// Allocates ~700MB then sleeps 10s if it survives — past our timeout,
	// so finishing early only happens if the memory cap actually killed it.
	cmd := `powershell -NoProfile -Command "$a = New-Object byte[] (700*1MB); for ($i=0; $i -lt $a.Length; $i += 4096) { $a[$i] = 1 }; Start-Sleep -Seconds 10"`

	start := time.Now()
	resp := runContained(cmd, 15*time.Second, 100)
	elapsed := time.Since(start)

	if elapsed >= 15*time.Second {
		t.Fatalf("hit the 15s timeout instead of failing fast on memory — job memory limit was not enforced (output: %q)", resp.Output)
	}
	// PowerShell exits 0 on allocation failure rather than being hard-killed,
	// so the signal here is OutOfMemoryException in the output, not exit code.
	if !strings.Contains(resp.Output, "OutOfMemoryException") {
		t.Errorf("expected the 700MB allocation to fail under the 100MB job limit, got: %q", resp.Output)
	}
	t.Logf("finished after %v, exit_code=%d, output=%q", elapsed, resp.ExitCode, resp.Output)
}
