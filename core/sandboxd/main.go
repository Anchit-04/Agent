// sandboxd is a long-lived local process that runs shell commands on behalf
// of the Python agent. This first version proves the plumbing only — it
// reads one JSON request per line from stdin, runs it, writes one JSON
// response per line back to stdout. No resource limits yet (that's the
// Job Object work, layered on top of this once the pipe itself is solid).
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Request is one line of JSON coming in from the caller (Python, eventually).
type Request struct {
	Command string `json:"command"`
}

// Response mirrors the shape agent.py's run_bash_command already returns,
// so swapping the Python implementation for a call into this binary later
// is a drop-in change — nothing above execute_tool() needs to know it moved.
type Response struct {
	ExitCode int    `json:"exit_code"`
	Output   string `json:"output"`
}

func main() {
	reader := bufio.NewReader(os.Stdin)

	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			// Most commonly: stdin closed (the caller exited or piped EOF).
			// That's a normal way for this process's life to end, not a crash.
			return
		}

		var req Request
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			writeResponse(Response{ExitCode: -1, Output: fmt.Sprintf("bad request JSON: %v", err)})
			continue
		}

		resp := runContained(req.Command, DefaultTimeout, DefaultMemoryLimitMB)
		writeResponse(resp)
	}
}

// Fixed defaults for now — per-request overrides (via the Request JSON) are
// a deliberate fast-follow, not done here, so this change stays scoped to
// "add containment" without also redesigning the wire protocol at the same time.
const (
	DefaultTimeout             = 30 * time.Second
	DefaultMemoryLimitMB int64 = 512
)

// runContained runs command with real OS-level containment via a Windows
// Job Object: a hard memory ceiling and a guaranteed kill of the entire
// process tree (including any children the command spawns) on timeout.
func runContained(command string, timeout time.Duration, memoryLimitMB int64) Response {
	cmd := exec.Command("cmd", "/C", command)

	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf

	if err := cmd.Start(); err != nil {
		return Response{ExitCode: -1, Output: fmt.Sprintf("failed to start command: %v", err)}
	}

	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return Response{ExitCode: -1, Output: fmt.Sprintf("failed to create job object: %v", err)}
	}
	defer windows.CloseHandle(job)

	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{
		BasicLimitInformation: windows.JOBOBJECT_BASIC_LIMIT_INFORMATION{
			LimitFlags: windows.JOB_OBJECT_LIMIT_JOB_MEMORY | windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
		},
		JobMemoryLimit: uintptr(memoryLimitMB * 1024 * 1024),
	}
	_, err = windows.SetInformationJobObject(
		job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)),
		uint32(unsafe.Sizeof(info)),
	)
	if err != nil {
		return Response{ExitCode: -1, Output: fmt.Sprintf("failed to configure job object: %v", err)}
	}

	// NOTE: there's a small window between cmd.Start() above and this
	// assignment where the process runs outside containment. A fully
	// race-free version would start it suspended (CREATE_SUSPENDED) and
	// resume only after assignment; for our threat model (containing our
	// own tool calls, not defeating an adversary racing this exact attach
	// step) this gap is an accepted, explicit trade-off, not an oversight.
	processHandle, err := windows.OpenProcess(
		windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE,
		false,
		uint32(cmd.Process.Pid),
	)
	if err != nil {
		return Response{ExitCode: -1, Output: fmt.Sprintf("failed to open process handle: %v", err)}
	}
	defer windows.CloseHandle(processHandle)

	if err := windows.AssignProcessToJobObject(job, processHandle); err != nil {
		return Response{ExitCode: -1, Output: fmt.Sprintf("failed to assign process to job: %v", err)}
	}

	done := make(chan error, 1)
	go func() {
		done <- cmd.Wait()
	}()

	select {
	case err := <-done:
		if err != nil {
			if exitErr, ok := err.(*exec.ExitError); ok {
				return Response{ExitCode: exitErr.ExitCode(), Output: buf.String()}
			}
			return Response{ExitCode: -1, Output: fmt.Sprintf("wait failed: %v", err)}
		}
		return Response{ExitCode: 0, Output: buf.String()}

	case <-time.After(timeout):
		windows.TerminateJobObject(job, 1)
		return Response{
			ExitCode: -1,
			Output:   fmt.Sprintf("command timed out after %v; output so far:\n%s", timeout, buf.String()),
		}
	}
}

func writeResponse(resp Response) {
	data, err := json.Marshal(resp)
	if err != nil {
		// Marshaling our own Response struct should never actually fail,
		// but silently dropping a response would hang the caller forever
		// waiting for a reply that's never coming — surface it instead.
		fmt.Fprintf(os.Stdout, `{"exit_code":-1,"output":"internal error marshaling response: %v"}`+"\n", err)
		return
	}
	fmt.Println(string(data))
}
