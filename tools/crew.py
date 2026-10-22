#!/usr/bin/env python3
"""Шифрование закрытой части сайта (сборы, карточки, фото).

Открытые данные никогда не лежат в репозитории: только crew/vault.json (зашифровано)
и crew/admin.json (список кодов, зашифрован кодом администратора).
Рабочая копия в открытом виде — папка plain/ (в .gitignore).

  python3 tools/crew.py init   --admin КОД? --people "Имя1,Имя2"   первый запуск, печатает коды
  python3 tools/crew.py unlock --code АДМИН-КОД                    vault -> plain/
  python3 tools/crew.py seal   --code АДМИН-КОД                    plain/ -> vault
  python3 tools/crew.py add    --code АДМИН-КОД --name "Имя"       выдать код новому человеку
  python3 tools/crew.py revoke --code АДМИН-КОД --name "Имя"       забрать доступ (меняет ключ)
  python3 tools/crew.py list   --code АДМИН-КОД                    кто имеет доступ

Формат совместим с WebCrypto в index.html: PBKDF2-SHA256 -> AES-256-GCM (iv 12 байт).
"""
import argparse, base64, hashlib, json, mimetypes, os, secrets, sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "crew" / "vault.json"
ADMIN = ROOT / "crew" / "admin.json"
PLAIN = ROOT / "plain"
ITER = 250_000
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ADMIN_LABEL = "__admin__"

b64 = lambda b: base64.b64encode(b).decode()
unb64 = base64.b64decode

def norm(code): return "".join(ch for ch in code.upper() if ch.isalnum())
def new_code(): c = "".join(secrets.choice(ALPHABET) for _ in range(12)); return f"{c[:4]}-{c[4:8]}-{c[8:]}"
def code_id(code): return hashlib.sha256(("drift:" + norm(code)).encode()).hexdigest()[:16]
def kek(code, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(norm(code).encode())
def seal_bytes(key, data):
    iv = secrets.token_bytes(12); return {"iv": b64(iv), "ct": b64(AESGCM(key).encrypt(iv, data, None))}
def open_bytes(key, box): return AESGCM(key).decrypt(unb64(box["iv"]), unb64(box["ct"]), None)
def wrap(K, code):
    salt = secrets.token_bytes(16); box = seal_bytes(kek(code, salt), K)
    return {"id": code_id(code), "salt": b64(salt), **box}

def load(p): return json.loads(p.read_text(encoding="utf-8"))
def save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

def key_from(code):
    v = load(VAULT); e = next((k for k in v["keys"] if k["id"] == code_id(code)), None)
    if not e: sys.exit("Код не найден в vault.json")
    return open_bytes(kek(code, unb64(e["salt"])), e)
def admin_codes(code):
    a = load(ADMIN); return json.loads(open_bytes(kek(code, unb64(a["salt"])), a))
def save_admin(code, codes):
    salt = secrets.token_bytes(16); save(ADMIN, {"salt": b64(salt), **seal_bytes(kek(code, salt), json.dumps(codes, ensure_ascii=False).encode())})

def build_payload():
    data = load(PLAIN / "data.json")
    for d in data.get("drivers", []):
        ph = d.get("photo")
        if ph and not ph.startswith("data:"):
            f = PLAIN / ph
            mime = mimetypes.guess_type(f.name)[0] or "image/jpeg"
            d["photo"] = f"data:{mime};base64," + b64(f.read_bytes())
    return json.dumps(data, ensure_ascii=False).encode()

def write_vault(K, codes):
    keys = [wrap(K, c) for c in codes.values()]
    save(VAULT, {"v": 1, "iter": ITER, "keys": keys, "payload": seal_bytes(K, build_payload())})

def cmd_init(a):
    if VAULT.exists(): sys.exit("vault.json уже есть — используй add/revoke")
    admin = a.admin or new_code()
    codes = {ADMIN_LABEL: admin}
    for name in [n.strip() for n in a.people.split(",") if n.strip()]: codes[name] = new_code()
    write_vault(secrets.token_bytes(32), codes); save_admin(admin, codes)
    for n, c in codes.items(): print(("АДМИН" if n == ADMIN_LABEL else n) + ":", c)

def cmd_unlock(a):
    K = key_from(a.code); data = json.loads(open_bytes(K, load(VAULT)["payload"]))
    (PLAIN / "photos").mkdir(parents=True, exist_ok=True)
    for i, d in enumerate(data.get("drivers", [])):
        ph = d.get("photo", "")
        if ph.startswith("data:"):
            head, raw = ph.split(",", 1); ext = ".png" if "png" in head else ".jpg"
            name = d.get("photoName") or f"photo{i}{ext}"
            (PLAIN / "photos" / name).write_bytes(unb64(raw)); d["photo"] = "photos/" + name; d["photoName"] = name
    save(PLAIN / "data.json", data)
    (PLAIN / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Открытая копия в", PLAIN)

def cmd_seal(a):
    K = key_from(a.code); v = load(VAULT); v["payload"] = seal_bytes(K, build_payload()); save(VAULT, v); print("vault.json обновлён")

def cmd_add(a):
    codes = admin_codes(a.code)
    if a.name in codes: sys.exit("Такое имя уже есть")
    K = key_from(a.code); c = new_code(); codes[a.name] = c
    v = load(VAULT); v["keys"].append(wrap(K, c)); save(VAULT, v); save_admin(a.code, codes); print(a.name + ":", c)

def cmd_revoke(a):
    codes = admin_codes(a.code)
    if a.name not in codes or a.name == ADMIN_LABEL: sys.exit("Нет такого человека")
    K = key_from(a.code); data = open_bytes(K, load(VAULT)["payload"])
    del codes[a.name]; K2 = secrets.token_bytes(32)
    save(VAULT, {"v": 1, "iter": ITER, "keys": [wrap(K2, c) for c in codes.values()], "payload": seal_bytes(K2, data)})
    save_admin(a.code, codes); print("Доступ отозван:", a.name)

def cmd_list(a):
    for n in admin_codes(a.code): print("АДМИН" if n == ADMIN_LABEL else n)

if __name__ == "__main__":
    p = argparse.ArgumentParser(); s = p.add_subparsers(dest="cmd", required=True)
    for n in ("init", "unlock", "seal", "add", "revoke", "list"):
        q = s.add_parser(n)
        if n == "init": q.add_argument("--admin"); q.add_argument("--people", default="")
        else: q.add_argument("--code", required=True)
        if n in ("add", "revoke"): q.add_argument("--name", required=True)
    a = p.parse_args(); globals()["cmd_" + a.cmd](a)
