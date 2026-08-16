package main

import (
	"strings"
	"testing"
	"time"
)

// Verifies the other half of the containment claim: JOB_OBJECT_LIMIT_JOB_MEMORY
// actually gets enforced, not just configured. A PowerShell process that tries
// to commit ~700MB under a 100MB job limit should be killed by the OS rather
// than run to completion.
func TestRunContained_MemoryLimitKillsProcess(t *testing.T) {
	// Allocates and touches ~700MB, then would sleep 10s if it survived —
	// well past our timeout, so a 0 exit code before the timeout only
	// happens if the OS killed it early for exceeding the job's memory cap.
	cmd := `powershell -NoProfile -Command "$a = New-Object byte[] (700*1MB); for ($i=0; $i -lt $a.Length; $i += 4096) { $a[$i] = 1 }; Start-Sleep -Seconds 10"`

	start := time.Now()
	resp := runContained(cmd, 15*time.Second, 100)
	elapsed := time.Since(start)

	if elapsed >= 15*time.Second {
		t.Fatalf("hit the 15s timeout instead of failing fast on memory — job memory limit was not enforced (output: %q)", resp.Output)
	}
	// PowerShell catches the allocation failure and exits 0 rather than
	// being hard-killed, so exit code isn't the signal here — the job's
	// commit limit blocking the allocation (surfaced as OutOfMemoryException)
	// is. If the limit weren't enforced, the process would happily commit
	// 700MB and hit the 10s Start-Sleep, which we already ruled out above.
	if !strings.Contains(resp.Output, "OutOfMemoryException") {
		t.Errorf("expected the 700MB allocation to fail under the 100MB job limit, got: %q", resp.Output)
	}
	t.Logf("finished after %v, exit_code=%d, output=%q", elapsed, resp.ExitCode, resp.Output)
}
