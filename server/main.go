// Phase 3: the dictionary API server.
//
// Per request: normalize -> ONE hashmap lookup -> send the stored gzip
// bytes as-is. No JSON parsing, no database, no compression at request
// time (except the rare non-gzip-client fallback).
//
// This does NOT use net/http's built-in server loop for writing responses.
// Go's http.ResponseWriter buffers through an internal ~2KB bufio.Writer,
// which would split most of our responses (commonly several KB) across
// multiple underlying socket writes -- exactly the Nagle/delayed-ACK stall
// pattern this project already hit once in a prototype. Instead: Go's
// battle-tested http.ReadRequest parses each request off a raw connection,
// and we build the ENTIRE response (status line + headers + body) into one
// buffer and hand it to conn.Write() in a single call, with TCP_NODELAY
// explicitly set on every connection as a second, independent guard against
// the same stall.
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	definePrefix = "/v1/define/"

	notFoundMaxAge = "3600"   // 1 hour, for 404s
	foundMaxAge    = "604800" // 1 week, for successful lookups
	idleTimeout    = 60 * time.Second
	writeTimeout   = 5 * time.Second
)

type server struct {
	idx         *dictIndex
	bin         *mappedFile
	etag        string // pre-formatted, quoted: `"<build_version>"`
	allowOrigin string
}

type httpResponse struct {
	status  string // e.g. "200 OK"
	headers []string
	body    []byte
}

func main() {
	dictDir := envOr("DICT_DIR", "../out")
	addr := ":" + envOr("PORT", "8080")
	allowOrigin := os.Getenv("ALLOW_ORIGIN")
	if allowOrigin == "" {
		log.Printf("warning: ALLOW_ORIGIN is not set; Access-Control-Allow-Origin will be omitted")
	}

	srv, err := newServer(dictDir, allowOrigin)
	if err != nil {
		log.Fatalf("%v", err)
	}

	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatalf("failed to listen on %s: %v", addr, err)
	}
	log.Printf("listening on %s (dict version %s, %d keys)", addr, srv.etag, len(srv.idx.Entries))

	srv.Serve(ln)
}

// newServer loads the index and mmaps the data file. Factored out of main()
// so tests can spin up a real server instance without duplicating startup
// logic.
func newServer(dictDir, allowOrigin string) (*server, error) {
	idxPath := filepath.Join(dictDir, "dict.idx")
	binPath := filepath.Join(dictDir, "dict.bin")

	t0 := time.Now()
	log.Printf("loading %s ...", idxPath)
	idx, err := loadIndex(idxPath)
	if err != nil {
		return nil, fmt.Errorf("failed to load %s: %w", idxPath, err)
	}
	log.Printf("loaded %d keys in %s", len(idx.Entries), time.Since(t0))

	log.Printf("mmapping %s ...", binPath)
	bin, err := mmapFile(binPath)
	if err != nil {
		return nil, fmt.Errorf("failed to mmap %s: %w", binPath, err)
	}
	log.Printf("mmapped %d bytes", len(bin.Data))

	return &server{
		idx:         idx,
		bin:         bin,
		etag:        `"` + strconv.FormatUint(idx.BuildVersion, 10) + `"`,
		allowOrigin: allowOrigin,
	}, nil
}

// Serve accepts connections until the listener is closed.
func (s *server) Serve(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			log.Printf("accept error: %v", err)
			continue
		}
		go s.handleConnection(conn)
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func (s *server) handleConnection(conn net.Conn) {
	defer conn.Close()
	if tc, ok := conn.(*net.TCPConn); ok {
		_ = tc.SetNoDelay(true)
	}

	reader := bufio.NewReader(conn)
	for {
		_ = conn.SetReadDeadline(time.Now().Add(idleTimeout))
		req, err := http.ReadRequest(reader)
		if err != nil {
			return // EOF, idle timeout, or malformed request: just close
		}

		resp := s.route(req)

		var out bytes.Buffer
		out.WriteString("HTTP/1.1 ")
		out.WriteString(resp.status)
		out.WriteString("\r\n")
		for _, h := range resp.headers {
			out.WriteString(h)
			out.WriteString("\r\n")
		}
		out.WriteString("Content-Length: ")
		out.WriteString(strconv.Itoa(len(resp.body)))
		out.WriteString("\r\n\r\n")
		if req.Method != http.MethodHead {
			out.Write(resp.body)
		}

		// Drain any request body (none expected for GET/HEAD, but a
		// misbehaving client sending one shouldn't desync the connection).
		if req.Body != nil {
			io.Copy(io.Discard, io.LimitReader(req.Body, 1<<20))
			req.Body.Close()
		}

		_ = conn.SetWriteDeadline(time.Now().Add(writeTimeout))
		if _, err := conn.Write(out.Bytes()); err != nil {
			return
		}

		if req.Close {
			return
		}
	}
}

func (s *server) route(req *http.Request) httpResponse {
	if req.Method != http.MethodGet && req.Method != http.MethodHead {
		return s.errorResponse("405 Method Not Allowed", []string{"Allow: GET, HEAD"}, `{"error":"method_not_allowed"}`)
	}

	path := req.URL.EscapedPath()

	if path == "/health" {
		return httpResponse{
			status:  "200 OK",
			headers: []string{"Content-Type: text/plain; charset=utf-8", "Cache-Control: no-store"},
			body:    []byte("ok"),
		}
	}

	if strings.HasPrefix(path, definePrefix) {
		rawWord := path[len(definePrefix):]
		return s.defineResponse(rawWord, req)
	}

	return s.errorResponse("404 Not Found", nil, `{"error":"not_found"}`, "Cache-Control: public, max-age="+notFoundMaxAge)
}

func (s *server) defineResponse(rawWord string, req *http.Request) httpResponse {
	key := normalize(rawWord)
	if !isValidKey(key) {
		return s.errorResponse("400 Bad Request", nil, `{"error":"invalid_word"}`)
	}

	entry, found := s.idx.Entries[key]
	if !found {
		return s.errorResponse("404 Not Found", nil, `{"error":"not_found"}`, "Cache-Control: public, max-age="+notFoundMaxAge)
	}

	if inm := req.Header.Get("If-None-Match"); inm != "" && etagMatches(inm, s.etag) {
		return httpResponse{
			status: "304 Not Modified",
			headers: []string{
				"Cache-Control: public, max-age=" + foundMaxAge,
				"ETag: " + s.etag,
				"Vary: Accept-Encoding",
			},
		}
	}

	gz := s.bin.Data[entry.Offset : entry.Offset+uint64(entry.Length)]

	headers := []string{
		"Content-Type: application/json; charset=utf-8",
		"Cache-Control: public, max-age=" + foundMaxAge,
		"ETag: " + s.etag,
		"Vary: Accept-Encoding",
	}
	if s.allowOrigin != "" {
		headers = append(headers, "Access-Control-Allow-Origin: "+s.allowOrigin)
	}

	if acceptsGzip(req.Header.Get("Accept-Encoding")) {
		headers = append(headers, "Content-Encoding: gzip")
		return httpResponse{status: "200 OK", headers: headers, body: gz}
	}

	// Rare fallback: client doesn't accept gzip. This is the one place
	// request-time computation happens, and the spec explicitly sanctions
	// it as an exception to "no compression at request time".
	raw, err := gunzip(gz)
	if err != nil {
		log.Printf("gunzip failed for key %q: %v", key, err)
		return s.errorResponse("500 Internal Server Error", nil, `{"error":"internal_error"}`)
	}
	return httpResponse{status: "200 OK", headers: headers, body: raw}
}

func (s *server) errorResponse(status string, headers []string, body string, extra ...string) httpResponse {
	h := []string{"Content-Type: application/json; charset=utf-8"}
	h = append(h, headers...)
	h = append(h, extra...)
	return httpResponse{status: status, headers: h, body: []byte(body)}
}

func acceptsGzip(acceptEncoding string) bool {
	if acceptEncoding == "" {
		return true // virtually every real client accepts gzip; treat absence as yes
	}
	return strings.Contains(strings.ToLower(acceptEncoding), "gzip")
}

// etagMatches handles a comma-separated If-None-Match list, "*", and
// optional weak-validator "W/" prefixes.
func etagMatches(ifNoneMatch, etag string) bool {
	for _, candidate := range strings.Split(ifNoneMatch, ",") {
		candidate = strings.TrimSpace(candidate)
		candidate = strings.TrimPrefix(candidate, "W/")
		if candidate == "*" || candidate == etag {
			return true
		}
	}
	return false
}

func gunzip(data []byte) ([]byte, error) {
	r, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return io.ReadAll(r)
}
