#!/usr/bin/env bash
# gambit installer — venv + python-chess, launcher in ~/.local/bin
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${HOME}/.local/bin"
mkdir -p "$BIN"

echo "› setting up venv"
python3 -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install --quiet --upgrade pip
"$HERE/.venv/bin/pip" install --quiet "chess>=1.10" pillow cairosvg

# generate the runtime ghostty config with absolute paths for this checkout
# (kept out of git; the tracked gambit.ghostty uses ${HOME}/chess-tui as default)
sed "s|\${HOME}/chess-tui|$HERE|g" "$HERE/gambit.ghostty" > "$HERE/.gambit.local.ghostty"

# launcher: open gambit in its own detached ghostty window (own config: no
# shaders / contrast clamping / single-instance), with an inline fallback when
# ghostty or a display isn't available.
cat > "$BIN/gambit" <<LAUNCH
#!/usr/bin/env bash
GCFG="$HERE/.gambit.local.ghostty"
if command -v ghostty >/dev/null 2>&1 && [ -n "\$WAYLAND_DISPLAY\$DISPLAY" ] && [ -z "\$GAMBIT_INLINE" ]; then
  setsid -f ghostty --config-default-files=false --config-file="\$GCFG" >/dev/null 2>&1 </dev/null
  exit 0
fi
exec "$HERE/.venv/bin/python" "$HERE/gambit.py" "\$@"
LAUNCH
chmod +x "$BIN/gambit"

if ! command -v stockfish >/dev/null 2>&1; then
  echo "⚠  stockfish not found. Engine modes need it:  yay -S stockfish"
else
  echo "✓ stockfish: $(command -v stockfish)"
fi
echo "✓ installed → $BIN/gambit   (make sure ~/.local/bin is on PATH)"
