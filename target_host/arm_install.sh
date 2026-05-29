#!/usr/bin/env bash
# =============================================================================
# LLM ETL — one-shot installer for ARM Linux (Debian/Ubuntu).
#
# What this does, in order:
#   1. Installs system packages (python3, build tools, cmake)
#   2. Creates user 'llm-etl' (unprivileged)
#   3. Sets up /opt/llm-etl/ layout
#   4. Compiles llama.cpp natively for ARM64 (NEON optimised) — ~10-30 min
#   5. Creates Python venv + pip install minimal deps
#   6. Installs three systemd services
#   7. Enables API + viewer (start on boot). LLM stays on-demand.
#   8. Smoke-test: curl localhost:8080/health
#
# Total disk usage: ~1.2 GB (model 940 MB + llama.cpp ~150 MB + Python venv ~80 MB)
# Total RAM usage at idle: ~250 MB (API ~150 + viewer ~100). LLM adds 1.5 GB on demand.
# =============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите от имени root: sudo bash $0" ; exit 1
fi

# Paths
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL=/opt/llm-etl
SVC_USER=llm-etl
SVC_GROUP=llm-etl

echo "================================================================"
echo " LLM ETL — установка на ARM"
echo "================================================================"
echo "  Исходная папка: $SRC"
echo "  Установка в:    $INSTALL"
echo ""

# ── 1. apt packages ─────────────────────────────────────────────────────────
echo "[1/9] Системные пакеты..."

# Detect distro + codename
. /etc/os-release
DISTRO_ID="${ID:-debian}"
CODENAME="${VERSION_CODENAME:-bullseye}"

# In Russia deb.debian.org (Fastly) is DPI-blocked. Test it with a short timeout;
# if unreachable, switch to a Russian mirror (mirror.yandex.ru) automatically.
echo "  проверяю доступность стандартного зеркала..."
NEED_RU_MIRROR=no
if ! curl -fsS --max-time 8 -o /dev/null "http://deb.debian.org/debian/dists/$CODENAME/Release" 2>/dev/null; then
    NEED_RU_MIRROR=yes
fi

if [ "$NEED_RU_MIRROR" = yes ]; then
    echo "  стандартное зеркало недоступно → переключаюсь на mirror.yandex.ru"
    # Backup original
    [ -f /etc/apt/sources.list ] && cp /etc/apt/sources.list /etc/apt/sources.list.bak
    if [ "$DISTRO_ID" = "ubuntu" ]; then
        cat > /etc/apt/sources.list <<EOF
deb http://mirror.yandex.ru/ubuntu $CODENAME main restricted universe multiverse
deb http://mirror.yandex.ru/ubuntu $CODENAME-updates main restricted universe multiverse
deb http://mirror.yandex.ru/ubuntu $CODENAME-security main restricted universe multiverse
EOF
    else
        cat > /etc/apt/sources.list <<EOF
deb http://mirror.yandex.ru/debian $CODENAME main contrib non-free
deb http://mirror.yandex.ru/debian $CODENAME-updates main contrib non-free
deb http://mirror.yandex.ru/debian-security $CODENAME-security main contrib non-free
EOF
    fi
    # Force IPv4 — IPv6 routing often broken / slow behind RU NAT
    echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99-force-ipv4
    echo 'Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/99-force-ipv4
else
    echo "  стандартное зеркало доступно — использую его"
fi

apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    git build-essential cmake \
    libgomp1 libstdc++6 pkg-config \
    curl ca-certificates

# ── 2. Service user ─────────────────────────────────────────────────────────
echo "[2/8] Пользователь $SVC_USER..."
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi

# ── 3. Layout ───────────────────────────────────────────────────────────────
echo "[3/9] Структура каталога..."
mkdir -p "$INSTALL"/{models,data/samples,parsers/{builtin,learned,shadow},logs}
# Copy app code (plain cp — no rsync dependency; clean __pycache__ afterwards)
cp -r "$SRC/core"        "$INSTALL/"
cp -r "$SRC/config"      "$INSTALL/"
cp -r "$SRC/target_host" "$INSTALL/"
find "$INSTALL" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
cp "$SRC/system_prompt.txt"      "$INSTALL/system_prompt.txt"
cp "$SRC/requirements-arm.txt"   "$INSTALL/requirements-arm.txt"
# Move model file
cp "$SRC/models/etl-parser-q4_k_m.gguf" "$INSTALL/models/"
ln -sf etl-parser-q4_k_m.gguf "$INSTALL/models/active.gguf"

# ── 4. Compile llama.cpp ────────────────────────────────────────────────────
echo "[4/9] Сборка llama.cpp под ARM (~10-30 минут)..."
if [ -x "$INSTALL/llama-server" ]; then
    echo "  llama-server уже собран — пропускаю компиляцию"
else
    LLAMA_SRC=$INSTALL/llama.cpp-src
    mkdir -p "$LLAMA_SRC"
    tar -xzf "$SRC/llama.cpp-source.tar.gz" -C "$LLAMA_SRC"
    cd "$LLAMA_SRC"
    # BUILD_SHARED_LIBS=OFF — собираем САМОДОСТАТОЧНЫЙ бинарь. Иначе llama-server
    # остаётся тонкой обёрткой (~15 КБ), которая при запуске ищет рядом
    # libllama-server-impl.so / libggml*.so, а после удаления build-папки их нет
    # → "error while loading shared libraries" (exit 127). Со статикой код вшит.
    # LTO=OFF и parallel=2 — чтобы линковка не упёрлась в память на 4-ГБ ARM.
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=ON -DGGML_LTO=OFF \
        -DLLAMA_CURL=OFF
    cmake --build build --config Release --parallel 2 --target llama-server llama-cli llama-quantize
    install -m 0755 build/bin/llama-server "$INSTALL/llama-server"
    install -m 0755 build/bin/llama-cli    "$INSTALL/llama-cli"
    cd /tmp ; rm -rf "$LLAMA_SRC"
    cd "$SRC"
fi

# ── 5. Python venv ──────────────────────────────────────────────────────────
echo "[5/9] Python venv + зависимости..."
python3 -m venv "$INSTALL/.venv"
PIP="$INSTALL/.venv/bin/pip"

if [ -d "$SRC/wheels" ] && [ -n "$(ls -A "$SRC/wheels" 2>/dev/null)" ]; then
    # OFFLINE install from bundled aarch64 wheels — works even with pypi.org blocked
    echo "  устанавливаю из вложенных wheels (offline, без интернета)..."
    "$PIP" install --no-index --find-links "$SRC/wheels" \
        -r "$INSTALL/requirements-arm.txt" 2>&1 | tail -3
else
    # Online fallback — try Russian PyPI mirror first, then default
    echo "  wheels не найдены — ставлю онлайн через зеркало..."
    "$PIP" install --upgrade pip --quiet 2>/dev/null || true
    "$PIP" install -r "$INSTALL/requirements-arm.txt" \
        --index-url https://mirror.yandex.ru/mirrors/pypi/simple/ \
        --trusted-host mirror.yandex.ru 2>&1 | tail -3 \
    || "$PIP" install -r "$INSTALL/requirements-arm.txt" 2>&1 | tail -3
fi

# ── 6. Permissions ──────────────────────────────────────────────────────────
echo "[6/8] Права доступа..."
chown -R "$SVC_USER:$SVC_GROUP" "$INSTALL"
chmod 0755 "$INSTALL"

# ── 6.5 Initialise SQLite database ──────────────────────────────────────────
echo "[6.5/9] Инициализация SQLite-БД..."
sudo -u "$SVC_USER" "$INSTALL/.venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, "/opt/llm-etl")
from core.db_writer_lite import init_db
result = init_db("/opt/llm-etl/data/etl.db", "/opt/llm-etl/config/schema.sql")
if result["created"]:
    print(f"  [OK] БД создана: {result['tables']} таблиц")
else:
    print(f"  [OK] БД уже существует: {result['tables']} таблиц")
PY

# ── 7. Systemd units ────────────────────────────────────────────────────────
echo "[7/9] Установка systemd-юнитов..."
for unit in llm-etl-api.service llm-etl-llm.service llm-etl-viewer.service llm-etl.target; do
    cp "$INSTALL/target_host/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable llm-etl-api.service llm-etl-viewer.service llm-etl.target
systemctl start  llm-etl-api.service llm-etl-viewer.service
# NB: llm-etl-llm.service intentionally NOT enabled — started on demand

# ── 8. Smoke check ──────────────────────────────────────────────────────────
echo "[8/9] Проверка сервисов..."
sleep 3
HOST_IP=$(hostname -I | awk '{print $1}')
api_ok=no
viewer_ok=no
for i in 1 2 3 4 5; do
    if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then api_ok=yes; fi
    if curl -fsS http://127.0.0.1:8081/health >/dev/null 2>&1; then viewer_ok=yes; fi
    [ "$api_ok" = yes ] && [ "$viewer_ok" = yes ] && break
    sleep 2
done

echo ""
echo "================================================================"
echo " ГОТОВО"
echo "================================================================"
echo ""
echo "[9/9] Готово."
echo ""
echo " API:    http://127.0.0.1:8080   ($([ "$api_ok" = yes ] && echo OK || echo НЕТ))"
echo " Viewer: http://$HOST_IP:8081     ($([ "$viewer_ok" = yes ] && echo OK || echo НЕТ))"
echo " БД:     /opt/llm-etl/data/etl.db  (42 таблицы)"
echo ""
echo " Откройте в браузере http://$HOST_IP:8081 чтобы видеть статус и БД."
echo ""
echo " Команды управления:"
echo "   systemctl status llm-etl-api      # статус API"
echo "   systemctl status llm-etl-viewer   # статус веб-просмотрщика"
echo "   journalctl -u llm-etl-api -f      # лог API"
echo ""
echo " Тест — отправьте файл:"
echo "   curl -X POST -F file=@your_file.xml http://127.0.0.1:8080/ingest"
