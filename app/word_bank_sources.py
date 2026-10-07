"""Bounded, DNS-pinned HTTP reads for untrusted vocabulary sources."""
import http.client
import ipaddress
import json
import os
import socket
import ssl
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_BYTES = 2_000_000
MAX_FILES = 20
MAX_ENTRIES = 5000


def checked_url(url):
    if len(url) > 2048 or any(ord(c) < 32 for c in url):
        raise ValueError("Source URL is too long or contains control characters")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use an HTTP(S) URL without embedded credentials")
    if parts.port not in {None, 80, 443, 8790}:
        raise ValueError("Source port must be 80, 443 or 8790")
    if parts.query or parts.fragment:
        raise ValueError("Use a raw source URL without query credentials or a fragment")
    return parts


def source_addresses(parts):
    allowed = {host.strip().lower() for host in os.getenv("WORD_BANK_IMPORT_ALLOWED_HOSTS", "").split(",") if host.strip()}
    addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)))
    if not addresses:
        raise ValueError("Source hostname has no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_reserved:
            raise ValueError("Source resolves to a prohibited address")
        if not ip.is_global and parts.hostname.lower() not in allowed:
            raise ValueError("Private source hosts require WORD_BANK_IMPORT_ALLOWED_HOSTS configuration")
    return addresses


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, parts, address):
        super().__init__(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), timeout=20)
        self.parts, self.address = parts, address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        if self.parts.scheme == "https":
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def fetch(url, token="", budget=None):
    if len(token) > 512 or any(ord(c) < 32 for c in token):
        raise ValueError("Invalid source API token")
    budget = budget if budget is not None else [MAX_BYTES]
    original = checked_url(url)
    origin = (original.scheme, original.hostname, original.port)
    for _ in range(5):
        parts = checked_url(url)
        addresses = source_addresses(parts)
        headers = {"User-Agent": "Puzzary-word-bank-import/1", "Accept": "application/json,text/plain,text/csv", "Accept-Encoding": "identity"}
        if token and (parts.scheme, parts.hostname, parts.port) == origin:
            if parts.scheme != "https":
                raise ValueError("Authenticated source imports require HTTPS")
            headers["Authorization"] = f"Bearer {token}"
        connection = PinnedConnection(parts, addresses[0])
        try:
            path = urlunsplit(("", "", parts.path or "/", "", ""))
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Source redirect has no location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"Source returned HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError("Compressed source responses are not supported")
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > budget[0]):
                raise ValueError("Import content exceeds the 2 MB limit")
            body = response.read(budget[0] + 1)
            if len(body) > budget[0]:
                raise ValueError("Import content exceeds the 2 MB limit")
            budget[0] -= len(body)
            return {"url": url, "text": body.decode("utf-8-sig"), "content_type": response.getheader("Content-Type", "")}
        except (http.client.HTTPException, OSError, UnicodeError) as error:
            raise ValueError("Could not read UTF-8 content from the source") from error
        finally:
            connection.close()
    raise ValueError("Source redirected too many times")


def fetch_sources(url, token=""):
    parts = checked_url(url.strip())
    host = parts.hostname.lower()
    path = parts.path.strip("/").split("/")
    budget = [MAX_BYTES]
    if host in {"pastebin.com", "www.pastebin.com"} and len(path) == 1:
        return [fetch(f"https://pastebin.com/raw/{quote(path[0], safe='')}", token, budget)]
    if host == "github.com" and len(path) >= 2:
        owner, repo = path[:2]
        if len(path) >= 5 and path[2] == "blob":
            return [fetch(f"https://raw.githubusercontent.com/{owner}/{repo}/{'/'.join(path[3:])}", token, budget)]
        info = json.loads(fetch(f"https://api.github.com/repos/{owner}/{repo}", token, budget)["text"])
        if not isinstance(info, dict) or not isinstance(info.get("default_branch"), str):
            raise ValueError("GitHub returned invalid repository metadata")
        branch = path[3] if len(path) >= 4 and path[2] == "tree" else info["default_branch"]
        prefix = "/".join(path[4:]) if len(path) > 4 else ""
        pending, sources, visited = [(quote(branch, safe=""), "")], [], 0
        while pending and len(sources) < MAX_FILES and visited < MAX_FILES:
            ref, directory = pending.pop(0)
            listing = json.loads(fetch(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}", token, budget)["text"])
            if not isinstance(listing, dict) or not isinstance(listing.get("tree"), list) or listing.get("truncated"):
                raise ValueError("GitHub returned an invalid or truncated directory")
            visited += 1
            for item in listing.get("tree", []):
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise ValueError("GitHub returned invalid directory entries")
                file_path = f"{directory}/{item['path']}".strip("/")
                if item.get("type") == "tree" and (not prefix or prefix.startswith(file_path + "/") or file_path.startswith(prefix)):
                    pending.append((item["sha"], file_path))
                elif item.get("type") == "blob" and (not prefix or file_path.startswith(prefix + "/")) and file_path.lower().endswith((".json", ".csv", ".txt")):
                    source_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(branch, safe='')}/{quote(file_path, safe='/')}"
                    sources.append(fetch(source_url, token, budget))
                    if len(sources) >= MAX_FILES:
                        break
        if pending or len(sources) >= MAX_FILES:
            raise ValueError("Repository is too large; select a file or smaller directory")
        if not sources:
            raise ValueError("No TXT, CSV or JSON vocabulary files found")
        return sources
    if host == "gist.github.com" and path[-1]:
        info = json.loads(fetch(f"https://api.github.com/gists/{path[-1]}", token, budget)["text"])
        if not isinstance(info, dict) or not isinstance(info.get("files"), dict):
            raise ValueError("GitHub returned invalid Gist metadata")
        files = [item for item in info.get("files", {}).values() if item.get("filename", "").lower().endswith((".json", ".csv", ".txt"))]
        if not files or len(files) > MAX_FILES:
            raise ValueError("Gist must contain 1 to 20 vocabulary files")
        if any(urlsplit(item["raw_url"]).hostname != "gist.githubusercontent.com" for item in files):
            raise ValueError("Gist returned an unexpected raw source host")
        return [fetch(item["raw_url"], "", budget) for item in files]
    if parts.path in {"", "/"}:
        url = url.rstrip("/") + "/api/word-banks/export"
    return [fetch(url, token, budget)]
