// healthcheck GETs http://localhost:<port><path> and exits 0 on a 2xx answer.
// The SUNET/vc runtime image is distroless (no shell, no curl), so compose
// healthchecks for the vc services run this static binary, mounted from the
// vc-healthcheck volume, instead of curl.
package main

import (
	"fmt"
	"net/http"
	"os"
	"time"
)

func main() {
	if len(os.Args) < 2 || len(os.Args) > 3 {
		fmt.Fprintln(os.Stderr, "usage: healthcheck <port> [path]")
		os.Exit(2)
	}
	path := "/health"
	if len(os.Args) == 3 {
		path = os.Args[2]
	}
	c := http.Client{Timeout: 4 * time.Second}
	resp, err := c.Get("http://localhost:" + os.Args[1] + path)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		fmt.Fprintln(os.Stderr, "status", resp.Status)
		os.Exit(1)
	}
}
