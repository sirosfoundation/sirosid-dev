// Command healthcheck is a container healthcheck probe for images that ship
// no shell and no curl.
//
// The vc services are built from vc's dockerfiles/worker, whose runtime stage
// is gcr.io/distroless/cc-debian12. That image contains the service binary and
// nothing else, so the usual
//
//	test: ["CMD", "curl", "-sf", "http://localhost:9004/health"]
//
// cannot run: there is no curl to exec, and the check fails instantly with
// "executable file not found". Every vc service then sits unhealthy forever
// and everything gated on `condition: service_healthy` never starts, which
// reads as the stack hanging rather than as a missing binary.
//
// This probe is built static (CGO_ENABLED=0) for the Docker daemon's
// architecture and bind-mounted into each container, so it runs in a
// distroless image as well as in one with a shell. mini-oidc already uses the
// same shape, with a healthcheck binary baked into its own image.
//
// Usage:
//
//	healthcheck <port> [path]    # path defaults to /health
//
// It exits 0 when the endpoint answers 2xx, and 1 otherwise.
package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: healthcheck <port> [path]")
		os.Exit(2)
	}

	port := os.Args[1]
	path := "/health"

	if len(os.Args) > 2 {
		path = os.Args[2]
	}

	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}

	// The timeout stays under the compose healthcheck's own `timeout:` so a
	// hung endpoint is reported as unhealthy by this probe rather than killed
	// by Docker, which produces a clearer status line.
	client := &http.Client{Timeout: 3 * time.Second}

	url := "http://" + net.JoinHostPort("127.0.0.1", port) + path

	resp, err := client.Get(url)
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: %s: %v\n", url, err)
		os.Exit(1)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		fmt.Fprintf(os.Stderr, "healthcheck: %s: status %d\n", url, resp.StatusCode)
		os.Exit(1)
	}
}
