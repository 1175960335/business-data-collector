package jshttp

import (
	"bufio"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"sync"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Stub upstream HTTP proxy
// ---------------------------------------------------------------------------

// stubUpstreamHTTP is a controllable stand-in for a real upstream HTTP proxy.
// It records the Proxy-Authorization value it receives on both the plain-HTTP
// and the CONNECT paths so tests can assert on what was actually sent upstream.
type stubUpstreamHTTP struct {
	addr        string
	srv         *http.Server
	requireAuth bool
	user        string
	pass        string

	mu           sync.Mutex
	connectAuth  []string
	plainAuth    []string
	connectCount int
}

func newStubUpstreamHTTP(t *testing.T, requireAuth bool, user, pass string) *stubUpstreamHTTP {
	t.Helper()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	s := &stubUpstreamHTTP{
		addr:        ln.Addr().String(),
		requireAuth: requireAuth,
		user:        user,
		pass:        pass,
	}

	s.srv = &http.Server{Handler: http.HandlerFunc(s.handle)} //nolint:gosec // test stub

	go func() { _ = s.srv.Serve(ln) }()

	t.Cleanup(func() { _ = s.srv.Close() })

	return s
}

func (s *stubUpstreamHTTP) expectedAuth() string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(s.user+":"+s.pass))
}

func (s *stubUpstreamHTTP) handle(w http.ResponseWriter, r *http.Request) {
	auth := r.Header.Get("Proxy-Authorization")

	if r.Method == http.MethodConnect {
		s.mu.Lock()
		s.connectAuth = append(s.connectAuth, auth)
		s.connectCount++
		s.mu.Unlock()

		if s.requireAuth && auth != s.expectedAuth() {
			w.Header().Set("Proxy-Authenticate", `Basic realm="stub"`)
			w.WriteHeader(http.StatusProxyAuthRequired)

			return
		}

		hj, ok := w.(http.Hijacker)
		if !ok {
			http.Error(w, "no hijack support", http.StatusInternalServerError)

			return
		}

		conn, _, err := hj.Hijack()
		if err != nil {
			return
		}

		defer conn.Close()

		_, _ = conn.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n"))
		_, _ = io.Copy(conn, conn) // echo, so tunnels are observable

		return
	}

	s.mu.Lock()
	s.plainAuth = append(s.plainAuth, auth)
	s.mu.Unlock()

	if s.requireAuth && auth != s.expectedAuth() {
		w.Header().Set("Proxy-Authenticate", `Basic realm="stub"`)
		w.WriteHeader(http.StatusProxyAuthRequired)

		return
	}

	w.Header().Set("X-Stub-Upstream", "yes")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("stub-body"))
}

func (s *stubUpstreamHTTP) seenConnectAuth() []string {
	s.mu.Lock()
	defer s.mu.Unlock()

	return append([]string(nil), s.connectAuth...)
}

func (s *stubUpstreamHTTP) seenPlainAuth() []string {
	s.mu.Lock()
	defer s.mu.Unlock()

	return append([]string(nil), s.plainAuth...)
}

// ---------------------------------------------------------------------------
// Stub upstream SOCKS5 proxy (RFC 1928 / RFC 1929)
// ---------------------------------------------------------------------------

type stubSOCKS5 struct {
	addr        string
	ln          net.Listener
	requireAuth bool
	user        string
	pass        string

	mu      sync.Mutex
	methods [][]byte
}

func newStubSOCKS5(t *testing.T, requireAuth bool, user, pass string) *stubSOCKS5 {
	t.Helper()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	s := &stubSOCKS5{addr: ln.Addr().String(), ln: ln, requireAuth: requireAuth, user: user, pass: pass}

	go s.serve()

	t.Cleanup(func() { _ = ln.Close() })

	return s
}

func (s *stubSOCKS5) serve() {
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}

		go s.handle(conn)
	}
}

func (s *stubSOCKS5) handle(conn net.Conn) {
	defer conn.Close()

	br := bufio.NewReader(conn)

	head := make([]byte, 2)
	if _, err := io.ReadFull(br, head); err != nil || head[0] != 5 {
		return
	}

	methods := make([]byte, int(head[1]))
	if _, err := io.ReadFull(br, methods); err != nil {
		return
	}

	s.mu.Lock()
	s.methods = append(s.methods, append([]byte(nil), methods...))
	s.mu.Unlock()

	if s.requireAuth {
		if !s.negotiateUserPass(conn, br, methods) {
			return
		}
	} else {
		offered := false

		for _, m := range methods {
			if m == 0x00 {
				offered = true
			}
		}

		if !offered {
			_, _ = conn.Write([]byte{5, 0xff})

			return
		}

		_, _ = conn.Write([]byte{5, 0x00})
	}

	s.forward(conn, br)
}

// forward reads the SOCKS5 CONNECT request and pipes the connection to the
// requested target.
func (s *stubSOCKS5) forward(conn net.Conn, br *bufio.Reader) {
	req := make([]byte, 4)
	if _, err := io.ReadFull(br, req); err != nil || req[0] != 5 || req[1] != 1 {
		return
	}

	host, err := readSOCKS5Host(br, req[3])
	if err != nil {
		return
	}

	pb := make([]byte, 2)
	if _, err := io.ReadFull(br, pb); err != nil {
		return
	}

	port := int(pb[0])<<8 | int(pb[1])

	target, err := net.Dial("tcp", net.JoinHostPort(host, strconv.Itoa(port)))
	if err != nil {
		_, _ = conn.Write([]byte{5, 5, 0, 1, 0, 0, 0, 0, 0, 0})

		return
	}

	defer target.Close()

	_, _ = conn.Write([]byte{5, 0, 0, 1, 0, 0, 0, 0, 0, 0})

	done := make(chan struct{}, 1)

	go func() {
		_, _ = io.Copy(target, br) // br, so buffered bytes are not lost
		close(done)
	}()

	_, _ = io.Copy(conn, target)

	<-done
}

func (s *stubSOCKS5) negotiateUserPass(conn net.Conn, br *bufio.Reader, methods []byte) bool {
	offered := false

	for _, m := range methods {
		if m == 0x02 {
			offered = true
		}
	}

	if !offered {
		_, _ = conn.Write([]byte{5, 0xff})

		return false
	}

	_, _ = conn.Write([]byte{5, 0x02})

	ver := make([]byte, 1)
	if _, err := io.ReadFull(br, ver); err != nil || ver[0] != 1 {
		return false
	}

	ulen := make([]byte, 1)
	if _, err := io.ReadFull(br, ulen); err != nil {
		return false
	}

	uname := make([]byte, int(ulen[0]))
	if _, err := io.ReadFull(br, uname); err != nil {
		return false
	}

	plen := make([]byte, 1)
	if _, err := io.ReadFull(br, plen); err != nil {
		return false
	}

	passwd := make([]byte, int(plen[0]))
	if _, err := io.ReadFull(br, passwd); err != nil {
		return false
	}

	if string(uname) != s.user || string(passwd) != s.pass {
		_, _ = conn.Write([]byte{1, 1})

		return false
	}

	_, _ = conn.Write([]byte{1, 0})

	return true
}

func readSOCKS5Host(br *bufio.Reader, atyp byte) (string, error) {
	switch atyp {
	case 1:
		b := make([]byte, 4)
		if _, err := io.ReadFull(br, b); err != nil {
			return "", err
		}

		return net.IP(b).String(), nil
	case 4:
		b := make([]byte, 16)
		if _, err := io.ReadFull(br, b); err != nil {
			return "", err
		}

		return net.IP(b).String(), nil
	case 3:
		l := make([]byte, 1)
		if _, err := io.ReadFull(br, l); err != nil {
			return "", err
		}

		b := make([]byte, int(l[0]))
		if _, err := io.ReadFull(br, b); err != nil {
			return "", err
		}

		return string(b), nil
	default:
		return "", fmt.Errorf("unsupported atyp %d", atyp)
	}
}

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

// rawConnect performs a CONNECT against the local wrapper and returns the
// response without asserting on it, so callers can check failure statuses.
func rawConnect(t *testing.T, proxyAddr, target string) (net.Conn, *bufio.Reader, *http.Response) {
	t.Helper()

	conn, err := net.DialTimeout("tcp", proxyAddr, 5*time.Second)
	if err != nil {
		t.Fatalf("dial wrapper: %v", err)
	}

	if err := conn.SetDeadline(time.Now().Add(15 * time.Second)); err != nil {
		_ = conn.Close()
		t.Fatalf("set deadline: %v", err)
	}

	_, err = fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target)
	if err != nil {
		_ = conn.Close()
		t.Fatalf("write CONNECT: %v", err)
	}

	br := bufio.NewReader(conn)

	resp, err := http.ReadResponse(br, &http.Request{Method: http.MethodConnect})
	if err != nil {
		_ = conn.Close()
		t.Fatalf("read CONNECT response: %v", err)
	}

	return conn, br, resp
}

// connectStatus performs a CONNECT against the local wrapper and returns the
// status code it produced, without keeping the tunnel open.
func connectStatus(t *testing.T, proxyAddr, target string) int {
	t.Helper()

	conn, _, resp := rawConnect(t, proxyAddr, target)

	defer conn.Close()

	defer resp.Body.Close()

	return resp.StatusCode
}

// openConnectTunnel performs a CONNECT and asserts the tunnel was established.
func openConnectTunnel(t *testing.T, proxyAddr, target string) (net.Conn, *bufio.Reader) {
	t.Helper()

	conn, br, resp := rawConnect(t, proxyAddr, target)

	if resp.StatusCode != http.StatusOK {
		_ = conn.Close()
		t.Fatalf("expected 200 from CONNECT, got %d", resp.StatusCode)
	}

	return conn, br
}

// newEchoServer starts a TCP server that echoes everything back.
func newEchoServer(t *testing.T) string {
	t.Helper()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	t.Cleanup(func() { _ = ln.Close() })

	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}

			go func() {
				defer c.Close()
				_, _ = io.Copy(c, c)
			}()
		}
	}()

	return ln.Addr().String()
}

// assertTunnelEchoes writes a payload through the tunnel and expects it back.
func assertTunnelEchoes(t *testing.T, conn net.Conn, br *bufio.Reader) {
	t.Helper()

	payload := "ping-through-tunnel"

	if _, err := conn.Write([]byte(payload)); err != nil {
		t.Fatalf("write through tunnel: %v", err)
	}

	buf := make([]byte, len(payload))
	if _, err := io.ReadFull(br, buf); err != nil {
		t.Fatalf("read through tunnel: %v", err)
	}

	if string(buf) != payload {
		t.Fatalf("expected %q echoed back, got %q", payload, string(buf))
	}
}

// ---------------------------------------------------------------------------
// A1 acceptance: unauthenticated upstream proxies
// ---------------------------------------------------------------------------

// TestStartAuthProxyAllowsUnauthenticatedUpstream is the direct regression for
// the reported defect: an upstream proxy without credentials used to be
// rejected outright with "username and password are required".
func TestStartAuthProxyAllowsUnauthenticatedUpstream(t *testing.T) {
	t.Parallel()

	upstream := newStubUpstreamHTTP(t, false, "", "")

	ap, err := StartAuthProxy("http://"+upstream.addr, "", "")
	if err != nil {
		t.Fatalf("expected an unauthenticated upstream to be accepted, got: %v", err)
	}

	defer ap.Close()

	if ap.hasAuth {
		t.Fatal("expected hasAuth to be false for an unauthenticated upstream")
	}
}

// TestStartAuthProxyRejectsPartialCredentials keeps the configuration mistake
// loud: one credential without the other is never intentional.
func TestStartAuthProxyRejectsPartialCredentials(t *testing.T) {
	t.Parallel()

	upstream := newStubUpstreamHTTP(t, false, "", "")

	if _, err := StartAuthProxy("http://"+upstream.addr, "user", ""); err == nil {
		t.Fatal("expected an error when only the username is set")
	}

	if _, err := StartAuthProxy("http://"+upstream.addr, "", "pass"); err == nil {
		t.Fatal("expected an error when only the password is set")
	}
}

// TestUnauthenticatedUpstreamSeesNoProxyAuthorization asserts on the wire
// behaviour, not just on the constructor: nothing that looks like a credential
// may be invented for an unauthenticated proxy.
func TestUnauthenticatedUpstreamSeesNoProxyAuthorization(t *testing.T) {
	t.Parallel()

	upstream := newStubUpstreamHTTP(t, false, "", "")

	ap, err := StartAuthProxy("http://"+upstream.addr, "", "")
	if err != nil {
		t.Fatalf("StartAuthProxy: %v", err)
	}

	defer ap.Close()

	resp, err := ap.HTTPClient().Get("http://plain-http-target.invalid/resource") //nolint:noctx // test
	if err != nil {
		t.Fatalf("GET through wrapper: %v", err)
	}

	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	if string(body) != "stub-body" {
		t.Fatalf("expected the upstream response body, got %q", string(body))
	}

	for _, got := range upstream.seenPlainAuth() {
		if got != "" {
			t.Fatalf("expected no Proxy-Authorization upstream, got %q", got)
		}
	}
}

// TestAuthenticatedUpstreamStillReceivesBasicAuthorization makes sure the
// existing authenticated behaviour was not broken by allowing the empty case.
func TestAuthenticatedUpstreamStillReceivesBasicAuthorization(t *testing.T) {
	t.Parallel()

	upstream := newStubUpstreamHTTP(t, true, "alice", "s3cret")

	ap, err := StartAuthProxy("http://"+upstream.addr, "alice", "s3cret")
	if err != nil {
		t.Fatalf("StartAuthProxy: %v", err)
	}

	defer ap.Close()

	resp, err := ap.HTTPClient().Get("http://plain-http-target.invalid/resource") //nolint:noctx // test
	if err != nil {
		t.Fatalf("GET through wrapper: %v", err)
	}

	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 from an authenticated upstream, got %d", resp.StatusCode)
	}

	seen := upstream.seenPlainAuth()
	if len(seen) != 1 || seen[0] != upstream.expectedAuth() {
		t.Fatalf("expected the upstream to receive %q, got %v", upstream.expectedAuth(), seen)
	}
}

// TestUnauthenticatedConnectTunnelWorks covers the HTTPS path, which is what
// Playwright actually uses. The target host is a reserved .invalid name so the
// test does not depend on real DNS resolution.
func TestUnauthenticatedConnectTunnelWorks(t *testing.T) {
	t.Parallel()

	upstream := newStubUpstreamHTTP(t, false, "", "")

	ap, err := StartAuthProxy("http://"+upstream.addr, "", "")
	if err != nil {
		t.Fatalf("StartAuthProxy: %v", err)
	}

	defer ap.Close()

	conn, br := openConnectTunnel(t, ap.Address(), "tunnel-target.invalid:443")

	defer conn.Close()

	assertTunnelEchoes(t, conn, br)

	for _, got := range upstream.seenConnectAuth() {
		if got != "" {
			t.Fatalf("expected no Proxy-Authorization on CONNECT, got %q", got)
		}
	}
}

// TestAuthenticatedConnectTunnelSendsBasicAuthorization is the CONNECT-side
// counterpart of the authenticated plain-HTTP test.
func TestAuthenticatedConnectTunnelSendsBasicAuthorization(t *testing.T) {
	t.Parallel()

	upstream := newStubUpstreamHTTP(t, true, "bob", "hunter2")

	ap, err := StartAuthProxy("http://"+upstream.addr, "bob", "hunter2")
	if err != nil {
		t.Fatalf("StartAuthProxy: %v", err)
	}

	defer ap.Close()

	conn, br := openConnectTunnel(t, ap.Address(), "tunnel-target.invalid:443")

	defer conn.Close()

	assertTunnelEchoes(t, conn, br)

	seen := upstream.seenConnectAuth()
	if len(seen) != 1 || seen[0] != upstream.expectedAuth() {
		t.Fatalf("expected CONNECT to carry %q, got %v", upstream.expectedAuth(), seen)
	}
}

// TestWrongCredentialsSurfaceProxyAuthRequired checks that a rejected upstream
// credential is reported as 407 rather than being swallowed or retried blindly.
func TestWrongCredentialsSurfaceProxyAuthRequired(t *testing.T) {
	t.Parallel()

	upstream := newStubUpstreamHTTP(t, true, "alice", "s3cret")

	ap, err := StartAuthProxy("http://"+upstream.addr, "alice", "wrong-password")
	if err != nil {
		t.Fatalf("StartAuthProxy: %v", err)
	}

	defer ap.Close()

	if got := connectStatus(t, ap.Address(), "tunnel-target.invalid:443"); got != http.StatusProxyAuthRequired {
		t.Fatalf("expected 407 for wrong credentials, got %d", got)
	}
}

// TestUnreachableUpstreamReportsBadGateway covers the "proxy not running" case.
func TestUnreachableUpstreamReportsBadGateway(t *testing.T) {
	t.Parallel()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	deadAddr := ln.Addr().String()

	if err := ln.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	ap, err := StartAuthProxy("http://"+deadAddr, "", "")
	if err != nil {
		t.Fatalf("StartAuthProxy: %v", err)
	}

	defer ap.Close()

	if got := connectStatus(t, ap.Address(), "tunnel-target.invalid:443"); got != http.StatusBadGateway {
		t.Fatalf("expected 502 for an unreachable upstream, got %d", got)
	}
}

// TestUnauthenticatedSOCKS5UpstreamWorks covers the SOCKS5 half of the
// requirement. socks5h is used so the private-network guard does not apply and
// the target can be a local echo server.
func TestUnauthenticatedSOCKS5UpstreamWorks(t *testing.T) {
	t.Parallel()

	target := newEchoServer(t)
	socks := newStubSOCKS5(t, false, "", "")

	ap, err := StartAuthProxy("socks5h://"+socks.addr, "", "")
	if err != nil {
		t.Fatalf("StartAuthProxy: %v", err)
	}

	defer ap.Close()

	if ap.proxyType != ProxyTypeSOCKS5 {
		t.Fatalf("expected SOCKS5 proxy type, got %v", ap.proxyType)
	}

	if ap.socks5Auth != nil {
		t.Fatal("expected no SOCKS5 auth for an unauthenticated upstream")
	}

	conn, br := openConnectTunnel(t, ap.Address(), target)

	defer conn.Close()

	assertTunnelEchoes(t, conn, br)
}

// TestAuthenticatedSOCKS5UpstreamStillAuthenticates guards the SOCKS5 auth path.
func TestAuthenticatedSOCKS5UpstreamStillAuthenticates(t *testing.T) {
	t.Parallel()

	target := newEchoServer(t)
	socks := newStubSOCKS5(t, true, "carol", "pw123")

	ap, err := StartAuthProxy("socks5h://"+socks.addr, "carol", "pw123")
	if err != nil {
		t.Fatalf("StartAuthProxy: %v", err)
	}

	defer ap.Close()

	if ap.socks5Auth == nil {
		t.Fatal("expected SOCKS5 auth to be configured")
	}

	conn, br := openConnectTunnel(t, ap.Address(), target)

	defer conn.Close()

	assertTunnelEchoes(t, conn, br)
}

// TestNewProxyPoolAcceptsUnauthenticatedProxy is the end-to-end entry point that
// google-maps-scraper actually calls when the user passes -proxies.
func TestNewProxyPoolAcceptsUnauthenticatedProxy(t *testing.T) {
	t.Parallel()

	upstream := newStubUpstreamHTTP(t, false, "", "")

	pool, err := NewProxyPool([]string{"http://" + upstream.addr})
	if err != nil {
		t.Fatalf("expected NewProxyPool to accept an unauthenticated proxy, got: %v", err)
	}

	defer func() {
		for _, p := range pool.proxies {
			_ = p.Close()
		}
	}()

	if got := pool.Next().Address(); got == "" {
		t.Fatal("expected a usable proxy address")
	}
}
