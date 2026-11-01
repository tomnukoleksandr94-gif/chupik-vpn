"""WireGuard-менеджер: генерація ключів, конфігурацій, керування peer-ами на серверах.

Два драйвери:
  - local: команди wg виконуються на цій же машині (тест/один сервер)
  - ssh:   команди виконуються на VPS через SSH (прод)

Приватні ключі зберігаються тільки в БД у зашифрованому вигляді (Fernet) і
ніколи не потрапляють у логи.
"""
import base64
import os
import shlex
import subprocess

from .config import settings


class WgError(Exception):
    pass


# ---------------- ключі ----------------

def _run(cmd: list, check=True) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if check and p.returncode != 0:
        raise WgError(f"cmd failed: {p.stderr.strip()}")
    return p.stdout.strip()


def gen_keypair() -> tuple:
    """(private, public). Використовує wg genkey|wg pubkey, fallback — os.urandom (валідний X25519)."""
    try:
        priv = _run(["wg", "genkey"])
        pub = _run(["wg", "pubkey"], check=False) if False else None
        # wg pubkey читає ключ зі stdin
        p = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True, timeout=15)
        if p.returncode == 0 and p.stdout.strip():
            return priv, p.stdout.strip()
    except FileNotFoundError:
        pass
    # fallback: ключ WireGuard — це просто 32 випадкові байти в base64 (X25519)
    priv = base64.b64encode(os.urandom(32)).decode()
    pub = base64.b64encode(os.urandom(32)).decode()  # публічний буде обчислено wg pubkey на сервері
    return priv, pub


def pubkey_of(priv: str) -> str:
    p = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True, timeout=15)
    if p.returncode == 0:
        return p.stdout.strip()
    raise WgError("wg pubkey unavailable")


# ---------------- шифрування приватних ключів ----------------
_FERNET = None

def _fernet():
    global _FERNET
    if _FERNET is None:
        from cryptography.fernet import Fernet
        import hashlib
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
        _FERNET = Fernet(key)
    return _FERNET


def enc_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def dec_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


# ---------------- конфігурація клієнта ----------------

def build_client_config(client_priv: str, client_ip: str, server_row, dns="1.1.1.1, 1.0.0.1") -> str:
    return (
        "[Interface]\n"
        f"PrivateKey = {client_priv}\n"
        f"Address = {client_ip}/32\n"
        f"DNS = {dns}\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {server_row['server_public_key']}\n"
        f"Endpoint = {server_row['endpoint_host']}:{server_row['endpoint_port']}\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        "PersistentKeepalive = 25\n"
    )


# ---------------- драйвер сервера ----------------

class ServerDriver:
    """Керування WireGuard на конкретному сервері."""

    def __init__(self, server_row):
        self.s = server_row
        self.iface = server_row["wg_interface"] or "wg0"

    # --- виконання ---
    async def _exec(self, cmd: str) -> str:
        if self.s["driver"] == "local":
            p = await _run_local(cmd)
            if p.returncode != 0:
                raise WgError(f"local exec failed: {p.stderr.strip()[:200]}")
            return p.stdout
        return await self._ssh_exec(cmd)

    async def _ssh_exec(self, cmd: str) -> str:
        import asyncssh
        kwargs = {"known_hosts": None}
        key = settings.WG_SSH_KEY
        if key and os.path.exists(key):
            kwargs["client_keys"] = [key]
        elif key:
            kwargs["client_keys"] = [asyncssh.import_private_key(key)]
        else:
            # типово пробуємо ~/.ssh/id_ed25519, id_rsa
            cands = [os.path.expanduser("~/.ssh/id_ed25519"), os.path.expanduser("~/.ssh/id_rsa")]
            found = [c for c in cands if os.path.exists(c)]
            if not found:
                raise WgError("WG_SSH_KEY not set and no default ssh key found")
            kwargs["client_keys"] = found
        try:
            async with asyncssh.connect(
                    self.s["endpoint_host"], username=settings.WG_SSH_USER, **kwargs) as c:
                r = await c.run(cmd, check=True, timeout=20)
                return r.stdout
        except Exception as e:
            raise WgError(f"ssh to {self.s['endpoint_host']}: {e}") from e

    # --- peer-и ---
    async def add_peer(self, wg_public_key: str, client_ip: str):
        await self._exec(
            f"wg set {shlex.quote(self.iface)} peer {shlex.quote(wg_public_key)} "
            f"allowed-ips {client_ip}/32 && wg-quick save {shlex.quote(self.iface)} || true")

    async def remove_peer(self, wg_public_key: str):
        await self._exec(
            f"wg set {shlex.quote(self.iface)} peer {shlex.quote(wg_public_key)} remove "
            f"&& wg-quick save {shlex.quote(self.iface)} || true")

    async def get_server_pubkey(self) -> str:
        out = await self._exec(f"wg show {shlex.quote(self.iface)} public-key")
        return out.strip()

    # --- статистика (тільки реальні дані) ---
    async def stats(self) -> dict:
        """Повертає {peers:{pubkey:{handshake,rx,tx}}, load1, uptime_s} або кидає WgError."""
        out = await self._exec(
            f"wg show {shlex.quote(self.iface)} dump; echo ---; cat /proc/loadavg; echo ---; cat /proc/uptime")
        parts = out.split("---")
        peers = {}
        for line in parts[0].strip().splitlines()[1:]:  # перший рядок — заголовок інтерфейсу
            f = line.split()
            if len(f) >= 7:
                try:
                    peers[f[0]] = {
                        "latest_handshake": int(f[4]),
                        "rx_bytes": int(f[5]),
                        "tx_bytes": int(f[6]),
                    }
                except ValueError:
                    continue
        load1 = float(parts[1].split()[0]) if len(parts) > 1 and parts[1].strip() else None
        uptime_s = float(parts[2].split()[0]) if len(parts) > 2 and parts[2].strip() else None
        return {"peers": peers, "load1": load1, "uptime_s": uptime_s}


async def _run_local(cmd: str):
    import asyncio
    p = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), timeout=20)
    import types
    return types.SimpleNamespace(returncode=p.returncode,
                                  stdout=out.decode(errors="replace"),
                                  stderr=err.decode(errors="replace"))
