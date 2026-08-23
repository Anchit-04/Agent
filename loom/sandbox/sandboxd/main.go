// sandboxd runs shell commands on behalf of the Python agent, contained in
// a Windows Job Object (hard memory ceiling, guaranteed process-tree kill
// on timeout). One JSON request per line in, one JSON response per line out.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

type Request struct {
	Command string `json:"command"`
}

type Response struct {
	ExitCode int    `json:"exit_code"`
	Output   string `json:"output"`
}

const MaxCapturedOutputBytes = 2 * 1024 * 1024 // 2MB

type limitedWriter struct {
	buf       bytes.Buffer
	limit     int
	truncated bool
}

func (w *limitedWriter) Write(p []byte) (int, error) {
	remaining := w.limit - w.buf.Len()
	if remaining <= 0 {
		w.truncated = true
		return len(p), nil
	}
	if len(p) > remaining {
		w.buf.Write(p[:remaining])
		w.truncated = true
		return len(p), nil
	}
	return w.buf.Write(p)
}

func (w *limitedWriter) String() string {
	s := w.buf.String()
	if w.truncated {
		s += fmt.Sprintf("\n... [output truncated at sandboxd's %dMB buffer cap]", MaxCapturedOutputBytes/(1024*1024))
	}
	return s
}

func main() {
	reader := bufio.NewReader(os.Stdin)

	for {
		line, err := reader.ReadString('\n')
		if err != nil {
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

const (
	DefaultTimeout             = 30 * time.Second
	DefaultMemoryLimitMB int64 = 512
)

func runContained(command string, timeout time.Duration, memoryLimitMB int64) Response {

	cmd := exec.Command("cmd")
	cmd.SysProcAttr = &syscall.SysProcAttr{CmdLine: `/C ` + command}

	buf := &limitedWriter{limit: MaxCapturedOutputBytes}
	cmd.Stdout = buf
	cmd.Stderr = buf

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
		fmt.Fprintf(os.Stdout, `{"exit_code":-1,"output":"internal error marshaling response: %v"}`+"\n", err)
		return
	}
	fmt.Println(string(data))
}
