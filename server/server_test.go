package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// testServer spins up a real server instance (real TCP listener, real
// handleConnection loop -- not net/http's test harness, since this server
// deliberately bypasses net/http's response writer) against the actual
// Phase 2 build output. Skips instead of failing if that build isn't
// present, so the pure unit tests (normalize_test.go, idx_test.go) still
// run in any environment.
func testServer(t *testing.T) (baseURL string, srv *server) {
	t.Helper()
	dictDir := "../out"
	if _, err := os.Stat(filepath.Join(dictDir, "dict.idx")); os.IsNotExist(err) {
		t.Skip("../out/dict.idx not present -- run the Phase 2 build first")
	}

	s, err := newServer(dictDir, "*")
	if err != nil {
		t.Fatalf("newServer: %v", err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	go s.Serve(ln)
	t.Cleanup(func() { ln.Close() })

	return "http://" + ln.Addr().String(), s
}

func TestHealth(t *testing.T) {
	base, _ := testServer(t)
	resp, err := http.Get(base + "/health")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Errorf("status = %d, want 200", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "ok" {
		t.Errorf("body = %q, want %q", body, "ok")
	}
}

func TestDefineFound(t *testing.T) {
	base, srv := testServer(t)
	resp, err := http.Get(base + "/v1/define/cat")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "application/json; charset=utf-8" {
		t.Errorf("Content-Type = %q", ct)
	}
	if cc := resp.Header.Get("Cache-Control"); cc != "public, max-age=604800" {
		t.Errorf("Cache-Control = %q", cc)
	}
	if resp.Header.Get("Vary") != "Accept-Encoding" {
		t.Errorf("Vary = %q, want Accept-Encoding", resp.Header.Get("Vary"))
	}
	if resp.Header.Get("Access-Control-Allow-Origin") != "*" {
		t.Errorf("Access-Control-Allow-Origin = %q, want *", resp.Header.Get("Access-Control-Allow-Origin"))
	}
	wantETag := srv.etag
	if resp.Header.Get("ETag") != wantETag {
		t.Errorf("ETag = %q, want %q", resp.Header.Get("ETag"), wantETag)
	}

	var payload struct {
		Query string `json:"query"`
	}
	// http.Client auto-decompresses gzip when we didn't set our own
	// Accept-Encoding, so resp.Body is already plain JSON here.
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if payload.Query != "cat" {
		t.Errorf("query = %q, want cat", payload.Query)
	}
}

func TestDefineNotFound(t *testing.T) {
	base, _ := testServer(t)
	resp, err := http.Get(base + "/v1/define/zzzznotarealword")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Fatalf("status = %d, want 404", resp.StatusCode)
	}
	if cc := resp.Header.Get("Cache-Control"); cc != "public, max-age=3600" {
		t.Errorf("Cache-Control = %q, want public, max-age=3600", cc)
	}
	var payload struct {
		Error string `json:"error"`
	}
	json.NewDecoder(resp.Body).Decode(&payload)
	if payload.Error != "not_found" {
		t.Errorf("error = %q, want not_found", payload.Error)
	}
}

func TestDefineInvalidWord(t *testing.T) {
	base, _ := testServer(t)
	cases := []string{"123", "%20%20", "..."}
	for _, w := range cases {
		resp, err := http.Get(base + "/v1/define/" + w)
		if err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != 400 {
			t.Errorf("word %q: status = %d, want 400", w, resp.StatusCode)
		}
		var payload struct {
			Error string `json:"error"`
		}
		json.NewDecoder(resp.Body).Decode(&payload)
		resp.Body.Close()
		if payload.Error != "invalid_word" {
			t.Errorf("word %q: error = %q, want invalid_word", w, payload.Error)
		}
	}
}

func TestMethodNotAllowed(t *testing.T) {
	base, _ := testServer(t)
	req, _ := http.NewRequest(http.MethodPost, base+"/v1/define/cat", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 405 {
		t.Fatalf("status = %d, want 405", resp.StatusCode)
	}
	if allow := resp.Header.Get("Allow"); allow != "GET, HEAD" {
		t.Errorf("Allow = %q, want %q", allow, "GET, HEAD")
	}
}

func TestConditionalGet304(t *testing.T) {
	base, srv := testServer(t)
	req, _ := http.NewRequest(http.MethodGet, base+"/v1/define/cat", nil)
	req.Header.Set("If-None-Match", srv.etag)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 304 {
		t.Fatalf("status = %d, want 304", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if len(body) != 0 {
		t.Errorf("304 body should be empty, got %d bytes", len(body))
	}
}

func TestGzipVsNoGzip(t *testing.T) {
	base, _ := testServer(t)

	// Explicitly request gzip: Go's Transport will NOT auto-decompress
	// when the caller sets its own Accept-Encoding header, so we can
	// inspect the raw wire bytes.
	req, _ := http.NewRequest(http.MethodGet, base+"/v1/define/mice", nil)
	req.Header.Set("Accept-Encoding", "gzip")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.Header.Get("Content-Encoding") != "gzip" {
		t.Errorf("Content-Encoding = %q, want gzip", resp.Header.Get("Content-Encoding"))
	}
	raw, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	gz, err := gzip.NewReader(bytes.NewReader(raw))
	if err != nil {
		t.Fatalf("response wasn't valid gzip: %v", err)
	}
	decoded, err := io.ReadAll(gz)
	if err != nil {
		t.Fatalf("gzip decode: %v", err)
	}
	if !bytes.Contains(decoded, []byte(`"query":"mice"`)) {
		t.Errorf("decoded body missing expected query field: %s", decoded[:min(200, len(decoded))])
	}

	// Now request identity (no gzip): the server must decompress server-side.
	req2, _ := http.NewRequest(http.MethodGet, base+"/v1/define/mice", nil)
	req2.Header.Set("Accept-Encoding", "identity")
	resp2, err := http.DefaultClient.Do(req2)
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if enc := resp2.Header.Get("Content-Encoding"); enc != "" {
		t.Errorf("Content-Encoding = %q, want empty (identity fallback)", enc)
	}
	plain, _ := io.ReadAll(resp2.Body)
	if !bytes.Equal(plain, decoded) {
		t.Errorf("identity-fallback body doesn't match gunzipped gzip body")
	}
}

func TestHeadRequest(t *testing.T) {
	base, _ := testServer(t)
	req, _ := http.NewRequest(http.MethodHead, base+"/v1/define/cat", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if len(body) != 0 {
		t.Errorf("HEAD response should have empty body, got %d bytes", len(body))
	}
	if resp.ContentLength <= 0 {
		t.Errorf("HEAD Content-Length = %d, want > 0 (same as GET would report)", resp.ContentLength)
	}
}

// TestFormResolution exercises the actual dictionary content: words that
// only exist as a form of another lemma must resolve to that lemma's
// meanings, tagged with "via".
func TestFormResolution(t *testing.T) {
	base, _ := testServer(t)
	cases := []struct {
		word       string
		wantVia    string
		wantInWord string // a word that should appear among the results
	}{
		{"went", "simple past of go", "go"},
		{"saw", "simple past of see", "see"},
		{"leaves", "plural of leaf", "leaf"},
	}
	for _, c := range cases {
		resp, err := http.Get(base + "/v1/define/" + c.word)
		if err != nil {
			t.Fatal(err)
		}
		var payload struct {
			Results []struct {
				Word string `json:"word"`
				Via  string `json:"via"`
			} `json:"results"`
		}
		json.NewDecoder(resp.Body).Decode(&payload)
		resp.Body.Close()

		found := false
		for _, r := range payload.Results {
			if r.Word == c.wantInWord && r.Via == c.wantVia {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("define(%q): expected a result word=%q via=%q, got %+v", c.word, c.wantInWord, c.wantVia, payload.Results)
		}
	}
}

// TestKeepAlive sends two requests over ONE TCP connection to make sure the
// hand-rolled connection loop actually supports persistent connections
// (required for the wrk benchmark and for real client performance).
func TestKeepAlive(t *testing.T) {
	base, _ := testServer(t)
	addr := base[len("http://"):]

	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	for i := 0; i < 2; i++ {
		req, _ := http.NewRequest(http.MethodGet, "/v1/define/cat", nil)
		req.Host = addr
		if err := req.Write(conn); err != nil {
			t.Fatalf("request %d write: %v", i, err)
		}
		resp, err := http.ReadResponse(bufio.NewReader(conn), req)
		if err != nil {
			t.Fatalf("request %d read response: %v", i, err)
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Errorf("request %d: status = %d, want 200", i, resp.StatusCode)
		}
	}
}
