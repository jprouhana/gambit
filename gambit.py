#!/usr/bin/env python3
"""gambit — a nocturne-style terminal chess coach.

Play vs Stockfish, analyse positions with a live eval bar, drill tactics
puzzles, and learn openings. Pure-stdlib ANSI rendering in the nocturne
house style; chess rules + UCI engine via python-chess.
"""
import os
import re
import io
import sys
import time
import base64
import select
import termios
import tty
import random
import shutil
import textwrap

try:
    import chess
    import chess.engine
except ImportError:
    sys.stderr.write("gambit needs python-chess.  pip install chess\n")
    sys.exit(1)

# ───────────────────────────── ansi / colour ─────────────────────────────
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITAL = "\x1b[3m"
VS_TEXT = "\uFE0E"   # variation selector-15: force monochrome text glyph
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ZW_RE = re.compile("[\uFE0E\uFE0F\u200B\u200C\u200D]")  # selectors / zero-width


def fg(c):
    return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"


def bg(c):
    return f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m"


def lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vis_len(s):
    return len(_ZW_RE.sub("", _ANSI_RE.sub("", s)))


def pad(s, w):
    """Pad a (possibly coloured) string to visible width w."""
    n = vis_len(s)
    if n >= w:
        return s
    return s + " " * (w - n)


def clip(s, w):
    out, count = [], 0
    i = 0
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        if count >= w:
            break
        out.append(s[i])
        count += 1
        i += 1
    return "".join(out)


# shared nocturne base palette
WHITE = (236, 236, 240)
GREY = (132, 132, 144)
DGREY = (74, 74, 86)
DARK = (24, 24, 30)
GREEN = (108, 222, 130)
RED = (240, 90, 95)

# (name, primary, secondary, accent)
THEMES = [
    ("gambit", (212, 175, 90), (96, 165, 250), (244, 114, 182)),
    ("synthwave", (255, 56, 222), (110, 80, 255), (0, 229, 255)),
    ("matrix", (0, 210, 70), (110, 255, 130), (200, 255, 210)),
    ("ocean", (0, 130, 255), (0, 215, 215), (160, 235, 255)),
    ("ember", (255, 80, 0), (255, 138, 41), (255, 120, 145)),
    ("ice", (120, 165, 255), (195, 220, 255), (245, 250, 255)),
    ("vesper", (124, 92, 255), (255, 94, 165), (120, 220, 255)),
]

# board squares are MID-tone so both pure-white and near-black pieces pop
# on either shade (monochrome glyphs only have one colour to work with).
SQ_LIGHT = (150, 156, 172)
SQ_DARK = (104, 110, 128)
PIECE_W = (255, 255, 255)
PIECE_B = (10, 10, 14)

# Swappable piece sets. The Unicode "white" codepoints (♔♕♖♗♘♙) are OUTLINE
# shapes and the "black" ones (♚♛♜♝♞♟) are SOLID — exactly the printed-chess
# convention, so white vs black is distinguished by SHAPE as well as colour.
GLYPH_W = {  # outline — white
    chess.PAWN: "♙", chess.KNIGHT: "♘", chess.BISHOP: "♗",
    chess.ROOK: "♖", chess.QUEEN: "♕", chess.KING: "♔",
}
GLYPH_B = {  # solid — black
    chess.PAWN: "♟", chess.KNIGHT: "♞", chess.BISHOP: "♝",
    chess.ROOK: "♜", chess.QUEEN: "♛", chess.KING: "♚",
}
# ASCII letters: always respect fg colour, can never be emoji-substituted.
LETTER = {
    chess.PAWN: "P", chess.KNIGHT: "N", chess.BISHOP: "B",
    chess.ROOK: "R", chess.QUEEN: "Q", chess.KING: "K",
}
# style -> human label (cycled with 'g'). "sprites" = kitty-graphics images.
PIECE_STYLES = ("sprites", "solid", "classic", "letters")
PIECE_LABELS = {
    "sprites": "graphic pieces",
    "classic": "classic (outline/solid)",
    "solid": "solid glyphs",
    "letters": "letters",
}
SYM_FONT_PATH = "/usr/share/fonts/noto/NotoSansSymbols2-Regular.ttf"


def piece_str(pc, style):
    """Display string for a piece in the given style (no colour)."""
    if style == "classic":
        return (GLYPH_W if pc.color == chess.WHITE else GLYPH_B)[pc.piece_type] + VS_TEXT
    if style == "solid":
        return GLYPH_B[pc.piece_type] + VS_TEXT
    return LETTER[pc.piece_type] if pc.color == chess.WHITE else LETTER[pc.piece_type].lower()


def _piece_png_glyph(piece_type, white, px):
    """Fallback piece image: Noto chess glyph silhouette + contrasting outline."""
    from PIL import Image, ImageDraw, ImageFont
    glyph = GLYPH_B[piece_type]
    font = ImageFont.truetype(SYM_FONT_PATH, int(px * 0.84))
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill, stroke = ((248, 249, 251, 255), (14, 14, 18, 255)) if white \
        else ((22, 22, 28, 255), (206, 210, 220, 255))
    d.text((px / 2, px / 2), glyph, font=font, fill=fill,
           stroke_width=max(2, px // 22), stroke_fill=stroke, anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def piece_png(piece_type, white, px=192):
    """Render a chess piece as a transparent RGBA PNG (bytes). Uses the
    imported cburnett vector set (via python-chess + cairosvg) with a
    contrasting outer halo so it reads on any square; falls back to a glyph
    silhouette if the SVG rasteriser is unavailable."""
    try:
        import chess.svg
        import cairosvg
        from PIL import Image, ImageFilter
        svg = chess.svg.piece(chess.Piece(piece_type, chess.WHITE if white else chess.BLACK))
        raw = cairosvg.svg2png(bytestring=svg.encode(), output_width=px, output_height=px)
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        alpha = img.split()[3]
        k = max(5, (px // 18) | 1)              # odd dilation kernel
        grow = alpha.filter(ImageFilter.MaxFilter(k))
        halo_col = (18, 18, 22, 255) if white else (236, 239, 246, 255)
        halo = Image.new("RGBA", img.size, halo_col)
        halo.putalpha(grow)
        out = Image.alpha_composite(halo, img)
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return _piece_png_glyph(piece_type, white, px)


def marker_png(ring, px=192):
    """Legal-move indicator centered on a transparent cell-sized canvas, so a
    full-cell kitty placement lands it dead-centre. ring=True → capture ring."""
    from PIL import Image, ImageDraw
    col = (118, 188, 255, 200)
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = px / 2
    if ring:                                    # hollow ring for captures
        r = px * 0.44
        d.ellipse([c - r, c - r, c + r, c + r], outline=col, width=max(3, px // 16))
    else:                                       # solid dot for quiet moves
        r = px * 0.16
        d.ellipse([c - r, c - r, c + r, c + r], fill=col)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


NAME = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}

# ───────────────────────────── engine ─────────────────────────────


def find_stockfish():
    cand = [shutil.which("stockfish"), "/usr/bin/stockfish", "/usr/local/bin/stockfish"]
    here = os.path.dirname(os.path.abspath(__file__))
    cand += [os.path.join(here, "stockfish"), os.path.join(here, "engine", "stockfish")]
    for c in cand:
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


class Engine:
    """Thin wrapper over a Stockfish UCI process."""

    def __init__(self, path, skill=None):
        self.path = path
        self.proc = chess.engine.SimpleEngine.popen_uci(path)
        if skill is not None:
            self.set_skill(skill)

    def set_skill(self, skill):
        try:
            self.proc.configure({"Skill Level": int(skill)})
        except Exception:
            pass

    def analyse(self, board, t=0.18, multipv=None):
        if multipv:
            return self.proc.analyse(board, chess.engine.Limit(time=t), multipv=multipv)
        return self.proc.analyse(board, chess.engine.Limit(time=t))

    def best(self, board, t=0.18):
        info = self.proc.analyse(board, chess.engine.Limit(time=t))
        pv = info.get("pv")
        return (pv[0] if pv else None), info

    def play(self, board, t=0.15):
        r = self.proc.play(board, chess.engine.Limit(time=t))
        return r.move

    def close(self):
        try:
            self.proc.quit()
        except Exception:
            pass


def _info0(info):
    return info[0] if isinstance(info, list) else info


def score_white_cp(info):
    """Centipawns from White's POV (mate scaled large)."""
    return _info0(info)["score"].white().score(mate_score=100000)


def score_pov_cp(info):
    """Centipawns from the side-to-move POV."""
    return _info0(info)["score"].relative.score(mate_score=100000)


def fmt_score(cp_white):
    if cp_white is None:
        return "  ?  "
    if abs(cp_white) >= 99000:
        n = (100000 - abs(cp_white))
        n = max(1, (n + 1) // 2)
        return ("#" if cp_white > 0 else "-#") + str(n)
    v = cp_white / 100.0
    return f"{v:+.2f}"


def win_frac(cp_white):
    cp = max(-1500, min(1500, cp_white))
    return 1.0 / (1.0 + 10 ** (-cp / 400.0))


# ───────────────────────────── difficulties ─────────────────────────────
# chess.com-style Elo ladder. Stockfish's UCI_Elo bottoms out around 1320, so
# weaker bots are emulated with a "handicap" of random blunders + shallow depth
# (see bot_move). elo=None ⇒ full strength.  (name, elo)
DIFFS = [
    ("Beginner", 250),
    ("Novice", 500),
    ("Casual", 800),
    ("Intermediate", 1100),
    ("Club", 1400),
    ("Skilled", 1700),
    ("Tough", 2100),
    ("Maximum", None),
]


def bot_move(engine, board, elo):
    """Pick a move at the requested Elo. Strong levels use Stockfish's own
    strength limiter; weak levels (sub-1320, below SF's floor) blunder on
    purpose and think shallowly so they actually play like a beginner."""
    import random
    if elo is None:                                   # full strength
        return engine.proc.play(board, chess.engine.Limit(time=0.30)).move
    if elo >= 1320:                                   # Stockfish's own limiter
        try:
            engine.proc.configure({"UCI_LimitStrength": True, "UCI_Elo": int(elo)})
        except Exception:
            pass
        return engine.proc.play(board, chess.engine.Limit(time=0.10)).move
    # handicap mode for true beginners
    legal = list(board.legal_moves)
    if not legal:
        return None
    blunder = max(0.0, min(0.55, (1250 - elo) / 2000.0))   # 250→0.5 … 1100→0.075
    if random.random() < blunder:
        # a "human" blunder: prefer a plausible-but-bad move (random capture or
        # quiet move) rather than the single worst move on the board
        caps = [m for m in legal if board.is_capture(m)]
        pool = caps + legal if (caps and random.random() < 0.4) else legal
        return random.choice(pool)
    depth = max(1, min(8, round(elo / 220)))               # 250→1 … 1100→5
    return engine.proc.play(board, chess.engine.Limit(depth=depth)).move

# move-quality thresholds in centipawns lost
QUALITY = [
    (0, "Best move", GREEN, "★"),
    (20, "Excellent", GREEN, "✓"),
    (60, "Good", (150, 200, 150), "·"),
    (120, "Inaccuracy", (230, 200, 90), "?!"),
    (300, "Mistake", (235, 150, 70), "?"),
    (10 ** 9, "Blunder", RED, "??"),
]


def classify(cp_loss, was_best):
    if was_best:
        return QUALITY[0]
    for thr, label, col, mark in QUALITY:
        if cp_loss <= thr:
            return (thr, label, col, mark)
    return QUALITY[-1]


# ───────────────────────────── content ─────────────────────────────
# Candidate tactical positions. Every one is validated by the engine at
# load time (must be legal + have a clearly winning best move), so flavour
# labels are cosmetic and a bad FEN is simply skipped.
PUZZLE_FENS = [
    # hand-built, instructive endgame/opening mates
    ("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1", "Back-rank mate"),
    ("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4", "Scholar's mate"),
    ("7k/8/6K1/8/8/8/8/7Q w - - 0 1", "Queen mate"),
    ("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1", "Box the king"),
    ("7k/6R1/8/8/8/8/8/R5K1 w - - 0 1", "Rook ladder"),
    ("8/8/8/4k3/8/2q5/4N3/4K3 w - - 0 1", "Knight fork"),
    # engine-mined & verified tactical shots
    ("1r2kb2/pp2qppr/B1p2Pnp/3ppb2/1n2P1P1/BPN2K1N/P1PP3P/R2Q3R w - - 2 14", "Win material"),
    ("r1b2r2/pBp1qpp1/2nk3p/2b1p3/2pPPN2/N4PP1/PP5n/R1BQR1K1 w - - 0 15", "Forced mate"),
    ("2b1kb2/rpppn1p1/p3p2r/5pqp/1PPnP3/P2P1PN1/3Q1KPP/RNB2B1R w - - 4 12", "Win the queen"),
    ("rnb1kbnr/p2p1ppp/1p2p3/2pN4/P7/RP1q4/2PPPPPP/2BQKBNR b Kkq - 2 7", "Win material"),
    ("rnbqkbnr/pp1pp3/5p1p/2p3p1/5NP1/5P1B/PPPPP2P/RNBQK2R b KQkq - 3 7", "Win a piece"),
    ("rn1q1bnr/Rbpkp1pp/3p4/1P3p2/4P3/2P4P/1P1P1P2/1NBQKBNR w K - 1 8", "Win material"),
    ("r2qkbnr/p1N2p1p/b1npp3/1pp3p1/8/2NPPP2/PPP3PP/R1BQKB1R b KQkq - 1 12", "Tactical shot"),
    ("r1b1k1nr/p1ppq3/1R1b4/4pppp/P1P5/N4PPP/2QPP1B1/2B1nKNR b kq - 2 14", "Win material"),
    ("rnbqkbnr/1Qp2pp1/4p3/p2p3p/5P2/2P5/PP1PP1PP/RNB1KBNR w KQkq - 0 5", "Grab the rook"),
    ("rnbqkbr1/4pppp/pQ2n3/3p4/8/6P1/PPPP1P1P/RNBK1BNR b q - 6 10", "Tactical shot"),
    # deeper / quieter shots (medium–hard)
    ("r1bqkbr1/p1pppp1p/1p2Q1pn/2n5/3P3P/4P2N/PPP1BPP1/RNB1K2R b KQq - 1 7", "Find the win"),
    ("1nb1kb1r/rp1ppppp/pqp4n/8/4P3/P2PBN2/1PP2PPP/RN1QKB1R w KQk - 3 6", "Find the win"),
    ("rnb1kBnr/1ppp1p1p/8/p5pP/1q1Pp3/2N3P1/RPP1PP1R/3QKBN1 w kq - 0 12", "Find the win"),
    ("rnb2bn1/1pp1kppr/3pq2p/p3p2P/2P3R1/1P3N2/P2PPPP1/RNBQKB2 b Q - 2 10", "Find the win"),
    ("rn1q1b1r/p2kp3/b1pp2pp/Pp6/Qn3pPP/N1PP1N2/RP2PP1K/2B2R2 b - - 2 15", "Find the win"),
    ("rnb1k1n1/p1qp4/1pp4r/4pppp/5P1P/PPbP2P1/2PQP2R/R1B1KBN1 b Qq - 0 11", "Find the win"),
    ("rnb2bnr/2pkp3/q6p/pp1p1pN1/NP6/P1P1P2P/3P1PP1/R1BQKBR1 w Q - 1 11", "Find the win"),
    ("rnb1k1nr/pp1qppbp/2p3p1/3pP2Q/3P4/1PN4N/P1P2PPP/R1B1KB1R b KQkq - 2 7", "Find the win"),
    ("rnbqkbnr/2p1p1Bp/3p1pp1/pp6/1P1P3P/2N5/P1P1PPP1/R2QKBNR b KQkq - 1 6", "Find the win"),
    ("r2qk1n1/1bppp2r/2n3pp/1p3Q2/p2PPP1b/4B1PB/PPP4P/1R2K1NR w K - 1 15", "Find the win"),
]

OPENINGS = [
    {
        "name": "Italian Game",
        "eco": "C50",
        "moves": ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6",
                  "d3", "d6", "O-O", "O-O", "Re1", "a6", "Bb3", "Ba7"],
        "idea": "Fast development hitting f7. Bishop on c4 eyes f7, c3+d3 prepare "
                "a big centre with d4. Both sides castle and tuck the bishop on "
                "a7/b3 before slowly building a kingside attack.",
    },
    {
        "name": "Ruy Lopez",
        "eco": "C60",
        "moves": ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6",
                  "O-O", "Be7", "Re1", "b5", "Bb3", "d6", "c3", "O-O"],
        "idea": "Pin the knight defending e5, then retreat the bishop to keep "
                "long-term pressure on c6. The Closed main line: White plays "
                "c3+d4 for the centre while Black expands with ...b5 and ...d6.",
    },
    {
        "name": "Sicilian Najdorf",
        "eco": "B90",
        "moves": ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6",
                  "Nc3", "a6", "Be2", "e5", "Nb3", "Be7", "O-O", "O-O"],
        "idea": "Black trades the c-pawn for the d-pawn for an open c-file and "
                "active play. ...a6 (the Najdorf) controls b5 and prepares ...e5 "
                "and queenside expansion — the sharpest, most-analysed Sicilian.",
    },
    {
        "name": "Queen's Gambit Declined",
        "eco": "D37",
        "moves": ["d4", "d5", "c4", "e6", "Nc3", "Nf6", "Bg5", "Be7",
                  "e3", "O-O", "Nf3", "h6", "Bh4", "b6", "cxd5", "Nxd5"],
        "idea": "Black declines the gambit and builds a solid but slightly "
                "cramped centre. White develops with Bg5+e3+Nf3; the ...b6 setup "
                "fianchettoes the light bishop to fight for the long diagonal.",
    },
    {
        "name": "French Defence",
        "eco": "C11",
        "moves": ["e4", "e6", "d4", "d5", "Nc3", "Nf6", "e5", "Nfd7",
                  "f4", "c5", "Nf3", "Nc6", "Be3", "cxd4", "Nxd4", "Bc5"],
        "idea": "Black accepts a cramped, solid position then strikes the centre "
                "with ...c5. White grabs space with e5+f4 (the Steinitz); the "
                "fight revolves around White's centre vs Black's queenside play.",
    },
    {
        "name": "Caro-Kann Defence",
        "eco": "B18",
        "moves": ["e4", "c6", "d4", "d5", "Nc3", "dxe4", "Nxe4", "Bf5",
                  "Ng3", "Bg6", "h4", "h6", "Nf3", "Nd7", "h5", "Bh7"],
        "idea": "A rock-solid reply to e4: Black gets the light-squared bishop "
                "out before ...e6 (unlike the French). The Classical main line — "
                "White gains kingside space with h4-h5 to harass the bishop.",
    },
    {
        "name": "King's Indian Defence",
        "eco": "E60",
        "moves": ["d4", "Nf6", "c4", "g6", "Nc3", "Bg7", "e4", "d6",
                  "Nf3", "O-O", "Be2", "e5", "O-O", "Nc6", "d5", "Ne7"],
        "idea": "Black lets White build a huge centre, then attacks it from the "
                "flank with the fianchettoed bishop and a kingside pawn storm "
                "(...f5-f4-g5). A fighting, asymmetrical defence.",
    },
    {
        "name": "London System",
        "eco": "D02",
        "moves": ["d4", "d5", "Nf3", "Nf6", "Bf4", "e6", "e3", "c5",
                  "c3", "Nc6", "Nbd2", "Bd6", "Bg3", "O-O", "Bd3", "b6"],
        "idea": "A reliable system you can play vs almost anything: Bf4, e3, Bd3, "
                "c3, Nbd2. Trade off Black's good bishop with Bg3, then aim for a "
                "kingside attack with Ne5 and the f/h-pawns.",
    },
]


# ───────────────────────────── app ─────────────────────────────
class Gambit:
    def __init__(self):
        self.theme_i = 0
        self.path = find_stockfish()
        self.fd = sys.stdin.fileno()
        self.old = None
        self.running = True
        self.cw = 6           # square width in cells (set by _fit)
        self.ch = 3           # square height in rows (set by _fit)
        self.stack = False    # panel below board instead of beside
        self.panel_w = 34
        self.cols = 100
        self.rows = 30
        self.piece_style = "sprites"  # graphic kitty pieces; toggle with 'g'
        self._sprites_ok = False

    def toggle_pieces(self):
        i = (PIECE_STYLES.index(self.piece_style) + 1) % len(PIECE_STYLES)
        self.piece_style = PIECE_STYLES[i]

    # adaptive layout — sizes the board to the live terminal so nothing ever
    # overflows or wraps, and the squares stay visually square. Terminal cells
    # are ~1.7x taller than wide, so a square needs cw ≈ ASPECT * ch columns.
    ASPECT = 1.6
    LW = 4                                  # rank-label gutter width
    CH_MAX = 2                              # cap cell height: a 1-char glyph
    CW_MAX = 3                              # can't fill a tall cell, so keep
    #                                         cells small and let font-size scale.

    def _fit(self):
        cols, rows = shutil.get_terminal_size((100, 30))
        self.cols, self.rows = cols, rows
        self.panel_w = max(22, min(34, cols - 40))
        self.stack = cols < (24 + 5 + self.panel_w + 12)
        chrome_v = 8                        # frame + help + title + padding
        avail_h = rows - chrome_v - (9 if self.stack else 0)
        avail_w = (cols - self.LW - 6) if self.stack else (cols - 5 - self.panel_w - 12)
        # Sprite (image) pieces scale to fill their cell, so let cells grow big.
        # Glyph pieces are one character, so cells are capped small (font scales).
        if self.piece_style == "sprites":
            chm, cwm, aspect = 8, 14, 1.9
        else:
            chm, cwm, aspect = self.CH_MAX, self.CW_MAX, self.ASPECT
        best = (2, 1)
        for ch in range(1, chm + 1):
            cw = max(2, min(cwm, round(ch * aspect)))
            if 8 * ch <= avail_h - 1 and 8 * cw + self.LW <= avail_w:
                best = (cw, ch)
        self.cw, self.ch = best

    # theme helpers ------------------------------------------------------
    @property
    def th(self):
        n, p, s, a = THEMES[self.theme_i]
        return {"name": n, "primary": p, "secondary": s, "accent": a}

    def cycle_theme(self):
        self.theme_i = (self.theme_i + 1) % len(THEMES)

    # terminal -----------------------------------------------------------
    def enter(self):
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self._sprites_ok = False        # fresh alt-screen: re-transmit images
        # alt screen, hide cursor, enable SGR mouse: button + drag-motion tracking
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?1002h\x1b[?1006h")
        sys.stdout.flush()

    def leave(self):
        # fully delete graphic pieces (d=A frees the stored images, not just
        # their placements), disable mouse, leave alt screen, restore cursor.
        sys.stdout.write("\x1b_Ga=d,d=A\x1b\\\x1b[?1002l\x1b[?1006l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        self._sprites_ok = False
        if self.old:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def _readb(self):
        return os.read(self.fd, 1).decode("utf-8", "ignore")

    def key(self, timeout=None):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        ch = self._readb()
        if ch == "\x1b":
            r, _, _ = select.select([self.fd], [], [], 0.02)
            if not r:
                return "ESC"
            b2 = self._readb()
            if b2 != "[":
                return "ESC"
            b3 = self._readb()
            if b3 == "<":            # SGR mouse: ESC [ < btn ; col ; row (M|m)
                buf = ""
                while True:
                    c = self._readb()
                    if c in ("M", "m") or c == "":
                        break
                    buf += c
                    if len(buf) > 20:
                        break
                try:
                    btn, col, row = (int(x) for x in buf.split(";"))
                except ValueError:
                    return None
                return ("MOUSE", btn, c == "M", col, row)
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(b3, "ESC")
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == "\x7f":
            return "BACKSPACE"
        if ch == "\x03":
            return "CTRL_C"
        return ch

    def wait_key(self):
        """Block for a key, but wake (returns None) when the terminal is
        resized so the caller's loop re-renders — keeps the view live."""
        base = shutil.get_terminal_size((100, 30))
        while True:
            k = self.key(0.3)
            if k is not None:
                return k
            if shutil.get_terminal_size((100, 30)) != base:
                return None

    def draw(self, lines):
        # hard guards: never emit a line wider than the terminal (prevents
        # wrap) and never more lines than fit (prevents scroll/top-cutoff).
        cols, rows = shutil.get_terminal_size((100, 30))
        out = ["\x1b[H"]
        for ln in lines[:rows]:
            out.append(clip(ln, cols) + RESET + "\x1b[K\r\n")
        out.append("\x1b[J")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # primitives ---------------------------------------------------------
    def frame(self, title, body, w):
        th = self.th
        fc = fg(lerp(DARK, th["primary"], 0.65))
        tc = fg(th["accent"]) + BOLD
        top = f"{fc}╭─ {tc}{title}{RESET}{fc} " + "─" * max(0, w - vis_len(title) - 5) + f"╮{RESET}"
        out = [top]
        for ln in body:
            out.append(f"{fc}│{RESET} " + pad(ln, w - 3) + f"{fc}│{RESET}")
        out.append(f"{fc}╰" + "─" * (w - 2) + f"╯{RESET}")
        return out

    def center(self, lines, total_h=None):
        cols, rows = shutil.get_terminal_size((100, 30))
        if total_h is None:
            total_h = rows
        top = max(0, (total_h - len(lines)) // 2)
        bw = max((vis_len(l) for l in lines), default=0)
        left = max(0, (cols - bw) // 2)
        self._pad = (top, left)             # remembered for mouse hit-testing
        pre = " " * left
        return [""] * top + [pre + l for l in lines]

    # ───────────────────────── board rendering ─────────────────────────
    def render_board(self, board, white_bottom=True, cursor=None, selected=None,
                     targets=None, last=None, hint=None, check_sq=None):
        th = self.th
        targets = targets or set()
        cw, ch = self.cw, self.ch
        lw = self.LW                # left gutter for rank labels
        gx = (cw - 1) // 2          # glyph column inside a cell
        gy = ch // 2                # glyph row inside a cell
        rows = []
        files = "abcdefgh"
        for vr in range(8):
            rank = (7 - vr) if white_bottom else vr
            label = str(rank + 1)
            cell_rows = [[] for _ in range(ch)]
            for vc in range(8):
                file = vc if white_bottom else (7 - vc)
                sq = chess.square(file, rank)
                light = (file + rank) % 2 == 1
                base = SQ_LIGHT if light else SQ_DARK
                pc = board.piece_at(sq)
                # pick the highlight hue + strength for this square (if any)
                hcolor, hstr = None, 0.0
                if last and sq in last:
                    hcolor, hstr = th["accent"], 0.30
                if hint and sq in hint:
                    hcolor, hstr = th["secondary"], 0.42
                if sq in targets:
                    hcolor, hstr = th["secondary"], 0.30
                if selected is not None and sq == selected:
                    hcolor, hstr = th["accent"], 0.62
                if check_sq is not None and sq == check_sq:
                    hcolor, hstr = RED, 0.55
                if cursor is not None and sq == cursor:
                    hcolor, hstr = th["primary"], 0.55
                sprites = self.piece_style == "sprites"
                if pc is not None and not sprites:
                    # Mild halo for a little pop. gambit's own terminal disables
                    # minimum-contrast, so piece colours render exactly — no
                    # heavy contrast tile needed; the checkerboard stays clean.
                    halo = (0, 0, 0) if pc.color == chess.WHITE else (255, 255, 255)
                    base = lerp(base, halo, 0.14)
                sqbg = lerp(base, hcolor, hstr) if hcolor else base
                for rr in range(ch):
                    if rr == gy and pc and not sprites:
                        gl = piece_str(pc, self.piece_style)
                        pcol = PIECE_W if pc.color == chess.WHITE else PIECE_B
                        s = " " * gx + gl + " " * (cw - gx - 1)
                        cell_rows[rr].append(f"{bg(sqbg)}{fg(pcol)}{BOLD}{s}{RESET}")
                    elif rr == gy and sq in targets and not pc and not sprites:
                        s = " " * gx + "•" + " " * (cw - gx - 1)
                        cell_rows[rr].append(f"{bg(sqbg)}{fg(th['secondary'])}{s}{RESET}")
                    else:
                        cell_rows[rr].append(f"{bg(sqbg)}{' ' * cw}{RESET}")
            for rr in range(ch):
                if rr == gy:
                    gutter = f"{fg(GREY)}{(' ' + label).center(lw)}{RESET}"
                else:
                    gutter = " " * lw
                rows.append(gutter + "".join(cell_rows[rr]))
        flabels = files if white_bottom else files[::-1]
        foot = " " * lw + "".join(f"{fg(GREY)}{c.center(cw)}{RESET}" for c in flabels)
        rows.append(foot)
        return rows

    def render_eval_bar(self, cp_white, white_bottom=True):
        th = self.th
        h = 8 * self.ch
        frac = win_frac(cp_white if cp_white is not None else 0)
        if not white_bottom:
            frac = 1 - frac
        filled = int(round(frac * h))
        light = (235, 236, 240)
        dark = (38, 40, 50)
        col = []
        col.append(f"{fg(GREY)} ev {RESET}")
        for i in range(h):
            from_bottom = (h - 1 - i)
            c = light if from_bottom < filled else dark
            col.append(f"{bg(c)}   {RESET}")
        col.append(f"{fg(th['accent'])}{fmt_score(cp_white):>4}{RESET}")
        return col

    def hjoin(self, *blocks, gap=2):
        h = max(len(b) for b in blocks)
        widths = [max((vis_len(l) for l in b), default=0) for b in blocks]
        out = []
        for i in range(h):
            parts = []
            for b, w in zip(blocks, widths):
                ln = b[i] if i < len(b) else ""
                parts.append(pad(ln, w))
            out.append((" " * gap).join(parts))
        return out

    def compose(self, title, boardb, evalb, panel, hints, white_bottom=True):
        """Lay board + eval + panel into a framed, centered, fit-to-window view.
        Side-by-side when wide enough, else board on top and panel below.
        Also records board screen geometry for mouse hit-testing."""
        gap = 2
        if self.stack:
            body = list(boardb)
            board_body_col = self.LW            # cells start after rank gutter
            if panel:
                body.append("")
                body += panel
        else:
            evalw = max((vis_len(l) for l in evalb), default=0) if evalb else 0
            blocks = [b for b in (evalb, boardb, panel) if b]
            body = self.hjoin(*blocks, gap=gap)
            board_body_col = (evalw + gap if evalb else 0) + self.LW
        w = max((vis_len(l) for l in body), default=10) + 3
        box = self.frame(title, body, w)
        lines = self.center(box + ["", hints])
        # board top-left cell, 1-based screen coords:
        #   center pad + frame "│ " (2) + body offset; box[0] is the top border.
        top, left = self._pad
        self._geom = {
            "row0": top + 2,                                  # box border + body[0]
            "col0": left + 2 + board_body_col + 1,
            "cw": self.cw, "ch": self.ch,
            "white_bottom": white_bottom, "n": 8,
        }
        return lines

    def mouse_to_square(self, col, row):
        """Map 1-based terminal (col,row) to a board square, or None."""
        g = getattr(self, "_geom", None)
        if not g:
            return None
        rc, rr = col - g["col0"], row - g["row0"]
        if rc < 0 or rr < 0:
            return None
        vc, vr = rc // g["cw"], rr // g["ch"]
        if vc > 7 or vr > 7:
            return None
        return self.vis_to_sq(vr, vc, g["white_bottom"])

    # ─────────────────────── graphic (kitty) pieces ───────────────────────
    def _sprite_id(self, pc):
        return pc.piece_type + (0 if pc.color == chess.WHITE else 6)   # 1..12

    DOT_ID, RING_ID = 13, 14

    def _transmit(self, parts, iid, png):
        data = base64.standard_b64encode(png).decode("ascii")
        first = True
        while data:
            chunk, data = data[:4096], data[4096:]
            more = 1 if data else 0
            if first:
                parts.append(f"\x1b_Gi={iid},f=100,a=t,t=d,q=2,m={more};{chunk}\x1b\\")
                first = False
            else:
                parts.append(f"\x1b_Gm={more};{chunk}\x1b\\")

    def ensure_sprites(self):
        """Transmit the 12 piece images + 2 move markers once (kitty protocol)."""
        if getattr(self, "_sprites_ok", False):
            return True
        try:
            parts = []
            for white in (True, False):
                for pt in range(1, 7):
                    self._transmit(parts, pt + (0 if white else 6), piece_png(pt, white))
            self._transmit(parts, self.DOT_ID, marker_png(False))
            self._transmit(parts, self.RING_ID, marker_png(True))
            sys.stdout.write("".join(parts))
            sys.stdout.flush()
            self._sprites_ok = True
            return True
        except Exception:
            self.piece_style = "solid"        # graceful fallback
            return False

    def place_sprites(self, board, targets=None):
        g = getattr(self, "_geom", None)
        if not g:
            return
        targets = targets or set()
        out = ["\x1b_Ga=d\x1b\\"]              # clear previous placements
        cw, ch = g["cw"], g["ch"]

        def cell(sq):
            vr, vc = self.sq_to_vis(sq, g["white_bottom"])
            return g["row0"] + vr * ch, g["col0"] + vc * cw

        for sq in chess.SQUARES:               # pieces first
            pc = board.piece_at(sq)
            if not pc:
                continue
            srow, scol = cell(sq)
            if srow >= 1 and scol >= 1:
                out.append(f"\x1b[{srow};{scol}H"
                           f"\x1b_Ga=p,i={self._sprite_id(pc)},c={cw},r={ch},C=1,q=2\x1b\\")
        for sq in targets:                     # move markers on top (centered)
            srow, scol = cell(sq)
            if srow < 1 or scol < 1:
                continue
            iid = self.RING_ID if board.piece_at(sq) else self.DOT_ID
            out.append(f"\x1b[{srow};{scol}H"
                       f"\x1b_Ga=p,i={iid},c={cw},r={ch},C=1,q=2\x1b\\")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def clear_sprites(self):
        sys.stdout.write("\x1b_Ga=d\x1b\\")
        sys.stdout.flush()

    def _overlay(self, board, targets=None):
        """Place graphic pieces over the just-drawn board, or clear them."""
        if self.piece_style == "sprites" and self.ensure_sprites():
            self.place_sprites(board, targets)
        elif getattr(self, "_sprites_ok", False):
            self.clear_sprites()

    # ───────────────────────── menus ─────────────────────────
    def menu(self, title, items, subtitle=None):
        self.clear_sprites()        # wipe any graphic pieces left by a board screen
        sel = 0
        while True:
            th = self.th
            body = []
            if subtitle:
                body.append(f"{fg(GREY)}{subtitle}{RESET}")
                body.append("")
            for i, (label, desc) in enumerate(items):
                if i == sel:
                    mark = f"{fg(th['primary'])}▌{RESET}"
                    lab = f"{bg(lerp(DARK, th['primary'], 0.18))}{fg(WHITE)}{BOLD} {label} {RESET}"
                    body.append(f"{mark}{lab}")
                    if desc:
                        body.append(f"  {fg(GREY)}{ITAL}{desc}{RESET}")
                else:
                    body.append(f" {fg(WHITE)} {label}{RESET}")
                    if desc:
                        body.append(f"  {fg(DGREY)}{desc}{RESET}")
                body.append("")
            box = self.frame(title, body, 60)
            hint = self.help_line([("↑↓/jk", "move"), ("↵", "select"),
                                   ("t", "theme"), ("q/esc", "back")])
            lines = self.center(box + ["", hint], None)
            self.draw(lines)
            k = self.wait_key()
            if k in ("UP", "k"):
                sel = (sel - 1) % len(items)
            elif k in ("DOWN", "j"):
                sel = (sel + 1) % len(items)
            elif k == "ENTER":
                return sel
            elif k == "t":
                self.cycle_theme()
            elif k in ("q", "ESC", "CTRL_C"):
                return None

    def help_line(self, pairs):
        th = self.th
        chips = []
        for k, d in pairs:
            chips.append(f"{fg(th['secondary'])}{k}{RESET} {fg(GREY)}{d}{RESET}")
        return (f"  {fg(DGREY)}·  {RESET}").join(chips)

    def banner(self):
        th = self.th
        a, p = th["accent"], th["primary"]
        logo = [
            f"{fg(p)}{BOLD} ♛  g a m b i t{RESET}",
            f"{fg(GREY)}{ITAL}   a terminal chess coach{RESET}",
        ]
        return logo

    def toast(self, msg, w=60, secs=1.4):
        th = self.th
        box = self.frame("gambit", [f"{fg(th['accent'])}{msg}{RESET}"], w)
        self.draw(self.center(box))
        end = time.time() + secs
        while time.time() < end:
            if self.key(0.05):
                break

    # ───────────────────────── shared board input ─────────────────────────
    def pick_promotion(self):
        th = self.th
        opts = [("Queen", chess.QUEEN), ("Rook", chess.ROOK),
                ("Bishop", chess.BISHOP), ("Knight", chess.KNIGHT)]
        sel = 0
        while True:
            body = [f"{fg(GREY)}Promote pawn to:{RESET}", ""]
            for i, (label, pt) in enumerate(opts):
                g = GLYPH_B[pt]
                if i == sel:
                    body.append(f"{fg(th['primary'])}▌{RESET}{bg(lerp(DARK, th['primary'], 0.18))}{fg(WHITE)}{BOLD} {g} {label} {RESET}")
                else:
                    body.append(f"  {fg(WHITE)}{g} {label}{RESET}")
            box = self.frame("promotion", body, 40)
            self.draw(self.center(box))
            k = self.wait_key()
            if k in ("UP", "k", "LEFT", "h"):
                sel = (sel - 1) % 4
            elif k in ("DOWN", "j", "RIGHT", "l"):
                sel = (sel + 1) % 4
            elif k == "ENTER":
                return opts[sel][1]
            elif k in ("q", "ESC"):
                return chess.QUEEN

    def _mouse(self, k, white_bottom, selected, targets):
        """Translate a ('MOUSE',...) event into (vr, vc, synthetic_key).
        Left press = select/move; release on a legal target = drop (move);
        drag-motion = just move the cursor (hover). Returns (None,None,None)
        for off-board or non-left events so the loop ignores them."""
        _, btn, press, col, row = k
        sq = self.mouse_to_square(col, row)
        if sq is None or (btn & 3) != 0:
            return None, None, None
        vr, vc = self.sq_to_vis(sq, white_bottom)
        if btn & 32:                      # motion while dragging → hover only
            return vr, vc, None
        if press:                         # button down → select, or move-if-target
            return vr, vc, "ENTER"
        # button up: complete a drag onto a legal target, else keep selection
        if selected is not None and sq in targets and sq != selected:
            return vr, vc, "ENTER"
        return vr, vc, None

    def cursor_step(self, vr, vc, k):
        if k in ("UP", "k"):
            vr = max(0, vr - 1)
        elif k in ("DOWN", "j"):
            vr = min(7, vr + 1)
        elif k in ("LEFT", "h"):
            vc = max(0, vc - 1)
        elif k in ("RIGHT", "l"):
            vc = min(7, vc + 1)
        return vr, vc

    def vis_to_sq(self, vr, vc, white_bottom):
        rank = (7 - vr) if white_bottom else vr
        file = vc if white_bottom else (7 - vc)
        return chess.square(file, rank)

    def sq_to_vis(self, sq, white_bottom):
        file, rank = chess.square_file(sq), chess.square_rank(sq)
        vr = (7 - rank) if white_bottom else rank
        vc = file if white_bottom else (7 - file)
        return vr, vc

    def targets_for(self, board, sq):
        return {m.to_square for m in board.legal_moves if m.from_square == sq}

    def check_square(self, board):
        if board.is_check():
            return board.king(board.turn)
        return None

    # ───────────────────────── play vs engine ─────────────────────────
    def play_mode(self):
        if not self.path:
            self.toast("Stockfish not found — install it first.", secs=2.2)
            return
        di = self.menu("difficulty", [(d[0], "full strength" if d[1] is None else f"~{d[1]} Elo")
                                       for d in DIFFS], "How strong should the opponent be?")
        if di is None:
            return
        cs = self.menu("your colour", [("White", "you move first"),
                                       ("Black", "engine moves first"),
                                       ("Random", "coin flip")], None)
        if cs is None:
            return
        human_white = True if cs == 0 else (False if cs == 1 else random.random() < 0.5)
        name, elo = DIFFS[di]

        coach = Engine(self.path)
        bot = Engine(self.path)
        board = chess.Board()
        white_bottom = human_white
        vr, vc = (6, 4) if human_white else (1, 4)
        selected = None
        targets = set()
        last = None
        hint = None
        coach_lines = [f"{fg(GREY)}You are {'White' if human_white else 'Black'} vs {name}.{RESET}"]
        history = []
        cp_white = 0
        try:
            while self.running:
                if board.is_game_over():
                    self.show_result(board, white_bottom, coach_lines, history,
                                     cp_white, name, last)
                # engine's turn?
                human_turn = (board.turn == chess.WHITE) == human_white
                if not human_turn and not board.is_game_over():
                    self.render_play(board, white_bottom, None, None, set(), last, hint,
                                     coach_lines, history, cp_white, name, thinking=True)
                    mv = bot_move(bot, board, elo)
                    san = board.san(mv)
                    board.push(mv)
                    history.append(san)
                    last = {mv.from_square, mv.to_square}
                    try:
                        info = coach.analyse(board, 0.18)
                        cp_white = score_white_cp(info)
                    except Exception:
                        pass
                    continue

                self.render_play(board, white_bottom, cursor_sq(vr, vc, white_bottom, self),
                                 selected, targets, last, hint, coach_lines, history, cp_white, name)
                k = self.wait_key()
                if isinstance(k, tuple):
                    mvr, mvc, k = self._mouse(k, white_bottom, selected, targets)
                    if mvr is not None:
                        vr, vc = mvr, mvc
                    if k is None:
                        continue
                if k in ("q", "CTRL_C"):
                    if self.confirm_quit():
                        break
                elif k == "ESC":
                    selected, targets, hint = None, set(), None
                elif k == "t":
                    self.cycle_theme()
                elif k == "f":
                    white_bottom = not white_bottom
                    vr, vc = 7 - vr, 7 - vc
                elif k == "g":
                    self.toggle_pieces()
                elif k == "u":
                    if len(board.move_stack) >= 2:
                        board.pop(); board.pop()
                        history = history[:-2]
                        last = None; selected = None; targets = set(); hint = None
                        coach_lines.append(f"{fg(GREY)}↶ took back a move.{RESET}")
                elif k == "h":
                    hint = self.compute_hint(coach, board)
                elif k in ("UP", "DOWN", "LEFT", "RIGHT", "j", "k", "h", "l") and k not in ("h",):
                    vr, vc = self.cursor_step(vr, vc, k)
                elif k == "ENTER":
                    sq = cursor_sq(vr, vc, white_bottom, self)
                    res = self.handle_click(board, sq, selected, targets, human_white)
                    selected, targets, made = res
                    if made:
                        hint = None
                        mv = made
                        # coach evaluation BEFORE pushing
                        cl = self.coach_eval(coach, board, mv)
                        san = board.san(mv)
                        board.push(mv)
                        history.append(san)
                        last = {mv.from_square, mv.to_square}
                        coach_lines.append(cl)
                        try:
                            info = coach.analyse(board, 0.18)
                            cp_white = score_white_cp(info)
                        except Exception:
                            pass
        finally:
            # remember this game so the postgame trainer can review it
            self.last_game = {
                "uci": [m.uci() for m in board.move_stack],
                "human_white": human_white,
                "result": board.result() if board.is_game_over() else "*",
                "opp": name,
            }
            if board.move_stack:                       # archive as PGN for later analysis
                self.save_pgn(board, human_white, name)
            coach.close()
            bot.close()

    GAMES_DIR = os.path.expanduser("~/.local/share/gambit/games")

    def save_pgn(self, board, human_white, opp):
        """Write the game to ~/.local/share/gambit/games as standard PGN."""
        try:
            import chess.pgn
            import datetime
            os.makedirs(self.GAMES_DIR, exist_ok=True)
            game = chess.pgn.Game.from_board(board)
            now = datetime.datetime.now()
            result = board.result() if board.is_game_over() else "*"
            game.headers["Event"] = "gambit"
            game.headers["Site"] = "gambit (terminal)"
            game.headers["Date"] = now.strftime("%Y.%m.%d")
            game.headers["Round"] = "-"
            game.headers["White"] = "You" if human_white else f"Stockfish · {opp}"
            game.headers["Black"] = f"Stockfish · {opp}" if human_white else "You"
            game.headers["Result"] = result
            fn = now.strftime("%Y-%m-%d_%H%M%S") + f"_vs_{opp}.pgn"
            path = os.path.join(self.GAMES_DIR, fn)
            with open(path, "w") as f:
                print(game, file=f, end="\n\n")
            self.last_pgn = path
            return path
        except Exception:
            return None

    def confirm_quit(self):
        th = self.th
        self.clear_sprites()        # don't show graphic pieces behind the dialog
        box = self.frame("leave game?", [
            f"{fg(WHITE)}Quit this game and return to menu?{RESET}", "",
            self.help_line([("y", "yes"), ("n", "no")])], 50)
        self.draw(self.center(box))
        while True:
            k = self.wait_key()
            if k in ("y", "ENTER"):
                return True
            if k in ("n", "ESC", "q"):
                return False

    def handle_click(self, board, sq, selected, targets, human_white):
        """Returns (selected, targets, made_move_or_None)."""
        pc = board.piece_at(sq)
        if selected is None:
            if pc and (pc.color == chess.WHITE) == (board.turn == chess.WHITE) and \
               (pc.color == chess.WHITE) == human_white:
                return sq, self.targets_for(board, sq), None
            return None, set(), None
        # have a selection
        if sq == selected:
            return None, set(), None
        if sq in targets:
            mv = chess.Move(selected, sq)
            # promotion?
            if board.piece_at(selected) and board.piece_at(selected).piece_type == chess.PAWN \
               and chess.square_rank(sq) in (0, 7):
                promo = self.pick_promotion()
                mv = chess.Move(selected, sq, promotion=promo)
            if mv in board.legal_moves:
                return None, set(), mv
            return None, set(), None
        if pc and (pc.color == chess.WHITE) == (board.turn == chess.WHITE) and \
           (pc.color == chess.WHITE) == human_white:
            return sq, self.targets_for(board, sq), None
        return None, set(), None

    def compute_hint(self, eng, board):
        try:
            mv, info = eng.best(board, 0.22)
            return {mv.from_square, mv.to_square} if mv else None
        except Exception:
            return None

    def coach_eval(self, eng, board, mv):
        th = self.th
        try:
            info = eng.analyse(board, 0.20)
            best_cp = score_pov_cp(info)
            best_mv = info.get("pv", [None])[0]
            was_best = (best_mv == mv)
            best_san = board.san(best_mv) if best_mv else "?"
            played_san = board.san(mv)
            tmp = board.copy()
            tmp.push(mv)
            info2 = eng.analyse(tmp, 0.18)
            after_pov_opp = score_pov_cp(info2)  # opponent's pov
            played_cp = -after_pov_opp
            loss = max(0, best_cp - played_cp)
            _, label, col, mark = classify(loss, was_best)
            line = f"{fg(col)}{mark} {label}{RESET} {fg(WHITE)}{played_san}{RESET}"
            if not was_best and label in ("Mistake", "Blunder", "Inaccuracy"):
                line += f"  {fg(GREY)}better: {fg(th['secondary'])}{best_san}{RESET}"
            return line
        except Exception:
            return f"{fg(WHITE)}{board.san(mv)}{RESET}"

    def render_play(self, board, white_bottom, cursor, selected, targets, last, hint,
                    coach_lines, history, cp_white, opp, thinking=False, result=None):
        self._fit()
        boardb = self.render_board(board, white_bottom, cursor, selected, targets, last,
                                   hint, self.check_square(board))
        evalb = self.render_eval_bar(cp_white, white_bottom)
        panel = self.side_panel(board, coach_lines, history, cp_white, opp, thinking,
                                mode="play", result=result)
        if result:
            hints = self.help_line([("r", "review"), ("↵", "menu"), ("f", "flip"),
                                    ("g", self.piece_style), ("t", "theme")])
        else:
            hints = self.help_line([("↑↓←→", "move"), ("↵", "pick/play"),
                                    ("h", "hint"), ("u", "undo"), ("f", "flip"),
                                    ("g", self.piece_style), ("t", "theme"), ("q", "quit")])
        self.draw(self.compose(f"play · vs {opp}", boardb, evalb, panel, hints, white_bottom))
        self._overlay(board, targets)

    def side_panel(self, board, coach_lines, history, cp_white, opp, thinking, mode="play", result=None):
        th = self.th
        w = self.panel_w
        out = []
        turn = "White" if board.turn == chess.WHITE else "Black"
        tc = PIECE_W if board.turn == chess.WHITE else (170, 170, 185)
        status = f"{fg(tc)}●{RESET} {fg(WHITE)}{turn} to move{RESET}"
        if thinking:
            status = f"{fg(th['accent'])}● {opp} is thinking…{RESET}"
        if result:
            status = f"{fg(th['accent'])}{BOLD}✦ {result}{RESET}"
        if board.is_check() and not board.is_game_over():
            status += f"  {fg(RED)}check!{RESET}"
        out.append(status)
        out.append(f"{fg(GREY)}eval {fg(th['accent'])}{fmt_score(cp_white)}{RESET}  {self.eval_word(cp_white)}")
        out.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        # material
        out.append(self.material_line(board))
        out.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        out.append(f"{fg(th['primary'])}{BOLD}coach{RESET}")
        for ln in coach_lines[-6:]:
            for wl in self.wrap(ln, w - 2):
                out.append(wl)
        out.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        out.append(f"{fg(th['primary'])}{BOLD}moves{RESET}")
        out += self.movelist(history, w - 2)
        return out[:(12 if self.stack else 8 * self.ch + 1)]

    def eval_word(self, cp):
        if cp is None:
            return ""
        if abs(cp) >= 99000:
            return f"{fg(GREEN if cp>0 else RED)}forced mate{RESET}"
        v = cp / 100.0
        if abs(v) < 0.4:
            return f"{fg(GREY)}equal{RESET}"
        who = "White" if v > 0 else "Black"
        if abs(v) < 1.2:
            return f"{fg(GREY)}{who} slightly better{RESET}"
        if abs(v) < 3:
            return f"{fg(GREY)}{who} better{RESET}"
        return f"{fg(GREY)}{who} winning{RESET}"

    def material_line(self, board):
        vals = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
        capW, capB = [], []
        start = {chess.PAWN: 8, chess.KNIGHT: 2, chess.BISHOP: 2, chess.ROOK: 2, chess.QUEEN: 1}
        for pt in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN):
            wc = len(board.pieces(pt, chess.WHITE))
            bc = len(board.pieces(pt, chess.BLACK))
            for _ in range(start[pt] - bc):
                capW.append(GLYPH_B[pt] + VS_TEXT)  # white captured these black pieces
            for _ in range(start[pt] - wc):
                capB.append(GLYPH_W[pt] + VS_TEXT)
        score = 0
        for pt, v in vals.items():
            score += v * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
        wtxt = f"{fg(PIECE_W)}{''.join(capW)}{RESET}"
        adv = f" {fg(GREEN)}+{score}{RESET}" if score > 0 else (f" {fg(RED)}{score}{RESET}" if score < 0 else "")
        return f"{fg(GREY)}captured {RESET}{wtxt}{adv}"

    def movelist(self, history, w):
        rows = []
        line = ""
        for i in range(0, len(history), 2):
            num = i // 2 + 1
            wm = history[i]
            bm = history[i + 1] if i + 1 < len(history) else ""
            seg = f"{fg(DGREY)}{num:>2}.{RESET} {fg(WHITE)}{wm:<7}{RESET}{fg(GREY)}{bm:<7}{RESET}"
            rows.append(seg)
        return rows[-6:]

    def wrap(self, colored_line, w):
        plain = _ANSI_RE.sub("", colored_line)
        if len(plain) <= w:
            return [colored_line]
        # simple wrap losing colour on continuation; keep it readable
        wrapped = textwrap.wrap(plain, w) or [""]
        # re-apply nothing fancy; first line keeps colour prefix
        out = [colored_line[:colored_line.find(plain[:1]) if plain else 0] + ""]
        # fallback: just clip
        return [clip(colored_line, w)] + [f"{fg(GREY)}{x}{RESET}" for x in wrapped[1:]]

    def show_result(self, board, white_bottom, coach_lines, history, cp_white, opp, last):
        th = self.th
        res = board.result()
        if board.is_checkmate():
            winner = "White" if board.turn == chess.BLACK else "Black"
            msg = f"Checkmate — {winner} wins"
        elif board.is_stalemate():
            msg = "Stalemate — draw"
        elif board.is_insufficient_material():
            msg = "Draw — insufficient material"
        else:
            msg = f"Game over — {res}"
        # keep the final position on screen: append the result to the coach
        # panel and re-render the board (do NOT wipe it).
        marker = f"{fg(th['accent'])}{BOLD}★ {msg}{RESET}"
        if not coach_lines or coach_lines[-1] != marker:
            coach_lines.append(marker)
            coach_lines.append(f"{fg(GREY)}r review · ↵ menu{RESET}")
        while True:
            self.render_play(board, white_bottom, None, None, set(), last, None,
                             coach_lines, history, cp_white, opp, result=msg)
            k = self.wait_key()
            if isinstance(k, tuple):     # ignore mouse on the final screen
                continue
            if k == "t":
                self.cycle_theme(); continue
            if k == "g":
                self.toggle_pieces(); continue
            if k == "f":
                white_bottom = not white_bottom; continue
            if k == "r":                 # jump into the postgame review
                self._goto_review = True
                break
            if k in ("ENTER", "q", "ESC", "CTRL_C"):
                break
        if self.piece_style == "sprites":
            self.clear_sprites()
        raise _BackToMenu()

    # ───────────────────────── postgame review ─────────────────────────
    QCOL = {"Blunder": RED, "Mistake": (235, 150, 70), "Inaccuracy": (230, 200, 90),
            "Good": (150, 200, 150), "Excellent": GREEN, "Best move": GREEN}

    def review_mode(self):
        """Pick a saved game (PGN) and walk it move-by-move: grade each of YOUR
        moves and show the stronger line you should have played."""
        if not self.path:
            self.toast("Stockfish not found — install it first.", secs=2.2)
            return
        import glob
        import chess.pgn
        files = sorted(glob.glob(os.path.join(self.GAMES_DIR, "*.pgn")), reverse=True)
        if not files:
            self.toast("No saved games yet — play a game first.", secs=2.4)
            return
        items = []
        for f in files[:24]:
            try:
                with open(f) as fh:
                    h = chess.pgn.read_headers(fh)
                you = "White" if h.get("White") == "You" else "Black"
                opp = (h.get("Black") if you == "White" else h.get("White")) or "?"
                opp = opp.split("·")[-1].strip()
                items.append((f"{h.get('Date','?')} · you {you} · {h.get('Result','*')}",
                              f"vs {opp}"))
            except Exception:
                items.append((os.path.basename(f), ""))
        pick = self.menu("review a game", items, "Pick a saved game to analyse (newest first).")
        if pick is None:
            return
        try:
            with open(files[pick]) as fh:
                game = chess.pgn.read_game(fh)
            moves = list(game.mainline_moves())
        except Exception:
            self.toast("Couldn't read that PGN.", secs=2)
            return
        if not moves:
            self.toast("That game has no moves.", secs=2)
            return
        human_white = game.headers.get("White") == "You"
        opp = (game.headers.get("Black") if human_white else game.headers.get("White")) or "?"
        g = {"opp": opp.split("·")[-1].strip(), "result": game.headers.get("Result", "*"),
             "human_white": human_white}

        eng = Engine(self.path)
        try:
            board = chess.Board()
            positions = [board.copy()]
            for m in moves:
                board.push(m)
                positions.append(board.copy())
            plies, th = [], self.th
            for i in range(1, len(moves) + 1):
                bar = int(28 * i / max(1, len(moves)))
                self.draw(self.center(self.frame("game review", [
                    f"{fg(th['accent'])}analysing the game…{RESET}", "",
                    f"{fg(th['primary'])}{'█'*bar}{fg(DGREY)}{'░'*(28-bar)}{RESET}  {i}/{len(moves)}",
                    "", f"{fg(GREY)}press q to stop early{RESET}"], 52)))
                before, after, mv = positions[i - 1], positions[i], moves[i - 1]
                mover_white = before.turn == chess.WHITE
                info = eng.analyse(before, 0.16)
                best_cp = score_pov_cp(info)
                best_mv = info.get("pv", [None])[0]
                best_san = before.san(best_mv) if best_mv else "?"
                best_line = self.pv_to_san(before, info.get("pv", []), 5)
                info_after = eng.analyse(after, 0.12)
                played_cp = -score_pov_cp(info_after)
                loss = max(0, best_cp - played_cp)
                _, label, col, mark = classify(loss, best_mv == mv)
                plies.append(dict(san=before.san(mv), mv=mv, best_san=best_san, best_line=best_line,
                                  loss=loss, label=label, col=col, mark=mark,
                                  is_human=(mover_white == human_white), white=mover_white,
                                  best_mv=best_mv, cp_white=score_white_cp(info_after)))
                if self.key(0) in ("q", "ESC", "CTRL_C"):
                    break
            evals = [20] + [p["cp_white"] for p in plies]   # white-pov eval per position
            tally = {}
            for p in plies:
                if p["is_human"]:
                    tally[p["label"]] = tally.get(p["label"], 0) + 1
            self._review_loop(positions, plies, evals, human_white, tally, g)
        finally:
            eng.close()

    def _review_loop(self, positions, plies, evals, human_white, tally, g):
        n = len(plies)
        i = n                          # start at the final analysed position
        white_bottom = human_white

        def next_mistake(cur, step):
            j = cur + step
            while 1 <= j <= n:
                p = plies[j - 1]
                if p["is_human"] and p["loss"] >= 120:
                    return j
                j += step
            return cur
        while self.running:
            i = max(0, min(n, i))
            board = positions[i]
            hint = lastsq = None
            if i >= 1:
                p = plies[i - 1]
                lastsq = {p["mv"].from_square, p["mv"].to_square}
                if p["best_mv"]:
                    hint = {p["best_mv"].from_square, p["best_mv"].to_square}
            self._fit()
            boardb = self.render_board(board, white_bottom, None, None, set(), lastsq,
                                       hint, self.check_square(board))
            evalb = self.render_eval_bar(evals[i] if i < len(evals) else 0, white_bottom)
            panel = self._review_panel(plies, evals, i, human_white, tally, g)
            hints = self.help_line([("←→", "step"), ("m/M", "next/prev mistake"),
                                    ("home/end", "ends"), ("f", "flip"), ("q", "menu")])
            self.draw(self.compose("game review", boardb, evalb, panel, hints, white_bottom))
            self._overlay(board, set())
            k = self.wait_key()
            if isinstance(k, tuple):
                continue
            if k in ("q", "ESC", "CTRL_C"):
                return
            elif k in ("RIGHT", "l", "n"):
                i = min(n, i + 1)
            elif k in ("LEFT", "h", "b"):
                i = max(0, i - 1)
            elif k == "m":
                i = next_mistake(i, 1)
            elif k == "M":
                i = next_mistake(i, -1)
            elif k in ("g",):
                self.toggle_pieces()
            elif k == "f":
                white_bottom = not white_bottom

    def _review_panel(self, plies, evals, i, human_white, tally, g):
        th = self.th
        w = self.panel_w
        out = [f"{fg(th['primary'])}{BOLD}game review{RESET} {fg(GREY)}· vs {g.get('opp','?')}{RESET}",
               f"{fg(GREY)}you = {'White' if human_white else 'Black'} · result {g.get('result','*')}"
               f" · ply {i}/{len(plies)}{RESET}",
               f"{fg(DGREY)}" + "─" * (w - 2) + RESET]
        if i == 0:
            out.append(f"{fg(GREY)}Starting position.{RESET}")
            out.append(f"{fg(GREY)}{ITAL}→ step forward through the game.{RESET}")
        else:
            p = plies[i - 1]
            moveno = (i + 1) // 2
            dots = "." if p["white"] else "…"
            who = f"{fg(th['accent'])}you{RESET}" if p["is_human"] else f"{fg(GREY)}opponent{RESET}"
            out.append(f"{fg(DGREY)}{moveno}{dots}{RESET} {fg(WHITE)}{BOLD}{p['san']}{RESET}  {who}")
            # eval swing (white POV) before → after
            before_e, after_e = evals[i - 1], evals[i]
            out.append(f"{fg(GREY)}eval {fmt_score(before_e)} → "
                       f"{fg(th['accent'])}{fmt_score(after_e)}{RESET}")
            mark = f"{fg(p['col'])}{BOLD}{p['mark']} {p['label']}{RESET}"
            if p["is_human"] and 20 <= p["loss"] < 2000:
                mark += f"  {fg(GREY)}(-{p['loss']/100:.1f}){RESET}"
            out.append(mark)
            if p["is_human"] and p["label"] not in ("Best move", "Excellent") \
                    and p["best_san"] != p["san"]:
                out.append(f"{fg(GREY)}better was{RESET} {fg(th['secondary'])}{BOLD}{p['best_san']}{RESET}")
                for wl in textwrap.wrap("→ " + p["best_line"], w - 2):
                    out.append(f"{fg(DGREY)}{wl}{RESET}")
        out.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        out += self._review_movelist(plies, i, w)
        return out

    def _review_movelist(self, plies, cur, w, rows=10):
        """Windowed move list, one ply per row, colour-coded by quality, with
        the current ply highlighted — so blunders are easy to spot and jump to."""
        th = self.th
        out = [f"{fg(th['primary'])}{BOLD}moves{RESET} {fg(DGREY)}(✓ ok · ?! · ? · ??){RESET}"]
        lo = max(0, min(cur - 1 - rows // 2, len(plies) - rows))
        lo = max(0, lo)
        for idx in range(lo, min(len(plies), lo + rows)):
            p = plies[idx]
            moveno = idx // 2 + 1
            dots = "." if p["white"] else "…"
            col = p["col"] if p["is_human"] else GREY
            mk = p["mark"] if p["is_human"] else " "
            row = f"{fg(DGREY)}{moveno:>2}{dots}{RESET} {fg(col)}{p['san']:<7}{mk}{RESET}"
            if idx + 1 == cur:
                row = f"{bg(lerp(DARK, th['primary'], 0.30))}{fg(WHITE)}▌{clip(row, w-3)}{RESET}"
            out.append(row)
        return out

    # ───────────────────────── analysis board ─────────────────────────
    def analysis_mode(self):
        if not self.path:
            self.toast("Stockfish not found — install it first.", secs=2.2)
            return
        eng = Engine(self.path)
        board = chess.Board()
        white_bottom = True
        vr, vc = 6, 4
        selected, targets, last, hint = None, set(), None, None
        history = []
        cp_white = 0
        info_lines = [f"{fg(GREY)}Free analysis. Move any piece; the bar updates.{RESET}"]
        bestline = ""
        try:
            while self.running:
                try:
                    info = eng.analyse(board, 0.22, multipv=2)
                    if isinstance(info, list):
                        cp_white = score_white_cp(info[0])
                        pv = info[0].get("pv", [])
                        bestline = self.pv_to_san(board, pv, 5)
                    else:
                        cp_white = score_white_cp(info)
                        bestline = self.pv_to_san(board, info.get("pv", []), 5)
                except Exception:
                    pass
                cursor = cursor_sq(vr, vc, white_bottom, self)
                self.render_analysis(board, white_bottom, cursor, selected, targets, last, hint,
                                     cp_white, bestline, history)
                k = self.wait_key()
                if isinstance(k, tuple):
                    mvr, mvc, k = self._mouse(k, white_bottom, selected, targets)
                    if mvr is not None:
                        vr, vc = mvr, mvc
                    if k is None:
                        continue
                if k in ("q", "ESC", "CTRL_C"):
                    break
                elif k == "t":
                    self.cycle_theme()
                elif k == "f":
                    white_bottom = not white_bottom; vr, vc = 7 - vr, 7 - vc
                elif k == "g":
                    self.toggle_pieces()
                elif k == "u":
                    if board.move_stack:
                        board.pop(); history = history[:-1]
                        last = None; selected = None; targets = set(); hint = None
                elif k == "n":
                    board.reset(); history = []; last = None; selected = None; targets = set()
                elif k == "h":
                    try:
                        mv, _ = eng.best(board, 0.25)
                        hint = {mv.from_square, mv.to_square} if mv else None
                    except Exception:
                        pass
                elif k in ("UP", "DOWN", "LEFT", "RIGHT", "j", "k", "l"):
                    vr, vc = self.cursor_step(vr, vc, k)
                elif k == "ENTER":
                    sq = cursor_sq(vr, vc, white_bottom, self)
                    pc = board.piece_at(sq)
                    if selected is None:
                        if pc and pc.color == board.turn:
                            selected, targets = sq, self.targets_for(board, sq)
                    elif sq in targets:
                        mv = chess.Move(selected, sq)
                        if pc is None and board.piece_at(selected).piece_type == chess.PAWN and \
                           chess.square_rank(sq) in (0, 7):
                            mv = chess.Move(selected, sq, promotion=self.pick_promotion())
                        elif board.piece_at(selected).piece_type == chess.PAWN and chess.square_rank(sq) in (0, 7):
                            mv = chess.Move(selected, sq, promotion=self.pick_promotion())
                        if mv in board.legal_moves:
                            history.append(board.san(mv)); board.push(mv)
                            last = {mv.from_square, mv.to_square}
                        selected, targets, hint = None, set(), None
                    elif pc and pc.color == board.turn:
                        selected, targets = sq, self.targets_for(board, sq)
                    else:
                        selected, targets = None, set()
        finally:
            eng.close()

    def pv_to_san(self, board, pv, n):
        out, tmp = [], board.copy()
        for mv in pv[:n]:
            try:
                out.append(tmp.san(mv)); tmp.push(mv)
            except Exception:
                break
        return " ".join(out)

    def render_analysis(self, board, white_bottom, cursor, selected, targets, last, hint,
                        cp_white, bestline, history):
        th = self.th
        self._fit()
        boardb = self.render_board(board, white_bottom, cursor, selected, targets, last, hint,
                                   self.check_square(board))
        evalb = self.render_eval_bar(cp_white, white_bottom)
        w = self.panel_w
        panel = []
        turn = "White" if board.turn == chess.WHITE else "Black"
        panel.append(f"{fg(WHITE)}{turn} to move{RESET}")
        panel.append(f"{fg(GREY)}eval {fg(th['accent'])}{fmt_score(cp_white)}{RESET}  {self.eval_word(cp_white)}")
        panel.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        panel.append(f"{fg(th['primary'])}{BOLD}best line{RESET}")
        for wl in textwrap.wrap(bestline or "…", w - 2):
            panel.append(f"{fg(th['secondary'])}{wl}{RESET}")
        panel.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        panel.append(f"{fg(th['primary'])}{BOLD}moves{RESET}")
        panel += self.movelist(history, w - 2)
        hints = self.help_line([("↑↓←→", "move"), ("h", "best move"),
                                ("u", "undo"), ("n", "reset"), ("f", "flip"),
                                ("g", self.piece_style), ("t", "theme"), ("q", "back")])
        self.draw(self.compose("analysis", boardb, evalb, panel, hints, white_bottom))
        self._overlay(board, targets)

    # ───────────────────────── puzzles ─────────────────────────
    def load_puzzles(self, eng, want=None):
        """Verify each candidate with the engine and tag a difficulty.
        want = 'Easy'|'Medium'|'Hard' to filter, or None for all."""
        good = []
        for fen, label in PUZZLE_FENS:
            try:
                b = chess.Board(fen)
                if not b.is_valid():
                    continue
                info = eng.analyse(b, 0.30, multipv=2)
                infos = info if isinstance(info, list) else [info]
                best_mv = infos[0].get("pv", [None])[0]
                best_cp = score_pov_cp(infos[0])
                if best_mv is None:
                    continue
                second_cp = score_pov_cp(infos[1]) if len(infos) > 1 else -100000
                sc = infos[0]["score"].relative
                is_mate = sc.is_mate() and sc.mate() > 0
                if is_mate:
                    n = sc.mate()
                    diff = "Easy" if n <= 1 else ("Medium" if n == 2 else "Hard")
                elif best_cp >= 250 and (best_cp - second_cp) >= 150:
                    margin = best_cp - second_cp
                    if best_cp >= 550 and margin >= 350:
                        diff = "Easy"
                    elif best_cp >= 350 and margin >= 200:
                        diff = "Medium"
                    else:
                        diff = "Hard"
                else:
                    continue                          # not a clean tactic
                if want is None or diff == want:
                    good.append((fen, label, best_mv, best_cp, diff))
            except Exception:
                continue
        random.shuffle(good)
        return good

    def puzzle_mode(self):
        if not self.path:
            self.toast("Stockfish not found — install it first.", secs=2.2)
            return
        di = self.menu("puzzle difficulty", [
            ("Easy", "mate-in-1 & obvious wins"),
            ("Medium", "mate-in-2 & clear tactics"),
            ("Hard", "deeper / quieter shots"),
            ("All", "everything, shuffled"),
        ], "Pick how hard the tactics should be.")
        if di is None:
            return
        want = ["Easy", "Medium", "Hard", None][di]
        eng = Engine(self.path)
        try:
            self.draw(self.center(self.frame("puzzles", [f"{fg(self.th['accent'])}Verifying tactics with the engine…{RESET}"], 56)))
            puzzles = self.load_puzzles(eng, want)
            if not puzzles:
                self.toast(f"No {want or ''} puzzles available — try another difficulty.", secs=2.4)
                return
            idx = 0
            solved = 0
            while self.running and idx < len(puzzles):
                fen, label, sol_mv, sol_cp, diff = puzzles[idx]
                label = f"{label}  ·  {diff}"
                board = chess.Board(fen)
                white_bottom = board.turn == chess.WHITE
                vr, vc = 4, 4
                selected, targets, hint = None, set(), None
                status = f"{fg(GREY)}{('White' if board.turn==chess.WHITE else 'Black')} to move — find the best move.{RESET}"
                attempts = 0
                done = False
                while self.running and not done:
                    cursor = cursor_sq(vr, vc, white_bottom, self)
                    self.render_puzzle(board, white_bottom, cursor, selected, targets, hint,
                                       label, idx + 1, len(puzzles), solved, status, attempts)
                    k = self.wait_key()
                    if isinstance(k, tuple):
                        mvr, mvc, k = self._mouse(k, white_bottom, selected, targets)
                        if mvr is not None:
                            vr, vc = mvr, mvc
                        if k is None:
                            continue
                    if k in ("q", "ESC", "CTRL_C"):
                        return
                    elif k == "t":
                        self.cycle_theme()
                    elif k == "f":
                        white_bottom = not white_bottom; vr, vc = 7 - vr, 7 - vc
                    elif k == "g":
                        self.toggle_pieces()
                    elif k == "s":  # skip / show solution
                        hint = {sol_mv.from_square, sol_mv.to_square}
                        status = f"{fg(th_secondary(self))}Solution: {board.san(sol_mv)}{RESET}"
                    elif k == "n":
                        done = True
                    elif k == "h":
                        hint = {sol_mv.from_square}
                        status = f"{fg(GREY)}Hint: move the piece on {chess.square_name(sol_mv.from_square)}.{RESET}"
                    elif k in ("UP", "DOWN", "LEFT", "RIGHT", "j", "k", "l"):
                        vr, vc = self.cursor_step(vr, vc, k)
                    elif k == "ENTER":
                        sq = cursor_sq(vr, vc, white_bottom, self)
                        pc = board.piece_at(sq)
                        if selected is None:
                            if pc and pc.color == board.turn:
                                selected, targets = sq, self.targets_for(board, sq)
                        elif sq in targets:
                            mv = chess.Move(selected, sq)
                            if board.piece_at(selected).piece_type == chess.PAWN and chess.square_rank(sq) in (0, 7):
                                mv = chess.Move(selected, sq, promotion=self.pick_promotion())
                            attempts += 1
                            correct = self.puzzle_check(eng, board, mv, sol_mv, sol_cp)
                            san = board.san(mv)
                            if correct:
                                solved += 1
                                status = f"{fg(GREEN)}✓ {san} — correct! {RESET}{fg(GREY)}(↵ next){RESET}"
                                selected, targets = None, set()
                                hint = {mv.from_square, mv.to_square}
                                self.render_puzzle(board, white_bottom, None, None, set(), hint,
                                                   label, idx + 1, len(puzzles), solved, status, attempts)
                                self.wait_enter()
                                done = True
                            else:
                                status = f"{fg(RED)}✗ {san} isn't best. {RESET}{fg(GREY)}try again · h hint · s solution{RESET}"
                                selected, targets = None, set()
                        elif pc and pc.color == board.turn:
                            selected, targets = sq, self.targets_for(board, sq)
                        else:
                            selected, targets = None, set()
                idx += 1
            self.toast(f"Done — solved {solved}/{len(puzzles)}.", secs=2.4)
        finally:
            eng.close()

    def puzzle_check(self, eng, board, mv, sol_mv, sol_cp):
        if mv == sol_mv:
            return True
        try:
            tmp = board.copy(); tmp.push(mv)
            info = eng.analyse(tmp, 0.22)
            after = -score_pov_cp(info)  # back to mover's pov
            return after >= sol_cp - 60
        except Exception:
            return False

    def wait_enter(self):
        while True:
            k = self.wait_key()
            if k in ("ENTER", "q", "ESC", "n"):
                return

    def render_puzzle(self, board, white_bottom, cursor, selected, targets, hint,
                      label, num, total, solved, status, attempts):
        th = self.th
        self._fit()
        boardb = self.render_board(board, white_bottom, cursor, selected, targets, None, hint,
                                   self.check_square(board))
        w = self.panel_w
        panel = []
        panel.append(f"{fg(th['primary'])}{BOLD}{label}{RESET}")
        panel.append(f"{fg(GREY)}puzzle {num}/{total} · solved {fg(GREEN)}{solved}{RESET}")
        panel.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        for wl in textwrap.wrap(_ANSI_RE.sub('', status), w - 2)[:3]:
            panel.append(clip(status, w - 2) if len(wl) == len(_ANSI_RE.sub('', status)) else f"{fg(GREY)}{wl}{RESET}")
        panel.append("")
        panel.append(f"{fg(GREY)}attempts: {attempts}{RESET}")
        panel.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        panel.append(f"{fg(GREY)}{ITAL}Pick the strongest move.{RESET}")
        panel.append(f"{fg(GREY)}{ITAL}Engine grades your choice.{RESET}")
        hints = self.help_line([("↑↓←→", "move"), ("↵", "play"),
                                ("h", "hint"), ("s", "solution"), ("n", "skip"),
                                ("f", "flip"), ("g", self.piece_style), ("q", "back")])
        self.draw(self.compose("puzzles · tactics", boardb, None, panel, hints, white_bottom))
        self._overlay(board, targets)

    # ───────────────────────── openings ─────────────────────────
    def opening_mode(self):
        oi = self.menu("openings", [(o["name"], o["eco"] + " · " + " ".join(o["moves"][:4]) + "…")
                                    for o in OPENINGS], "Drill a line move by move.")
        if oi is None:
            return
        op = OPENINGS[oi]
        board = chess.Board()
        ply = 0
        white_bottom = True
        vr, vc = 6, 4
        selected, targets, hint = None, set(), None
        msg = f"{fg(GREY)}Play the next book move.{RESET}"
        while self.running:
            done = ply >= len(op["moves"])
            nextmv = None
            if not done:
                try:
                    nextmv = board.parse_san(op["moves"][ply])
                except Exception:
                    done = True
            cursor = cursor_sq(vr, vc, white_bottom, self)
            self.render_opening(board, white_bottom, cursor, selected, targets, hint, op, ply, msg, done)
            k = self.wait_key()
            if isinstance(k, tuple):
                mvr, mvc, k = self._mouse(k, white_bottom, selected, targets)
                if mvr is not None:
                    vr, vc = mvr, mvc
                if k is None:
                    continue
            if k in ("q", "ESC", "CTRL_C"):
                return
            elif k == "t":
                self.cycle_theme()
            elif k == "f":
                white_bottom = not white_bottom; vr, vc = 7 - vr, 7 - vc
            elif k == "g":
                self.toggle_pieces()
            elif k == "n":
                return
            elif k == "r":
                board.reset(); ply = 0; selected = None; targets = set(); hint = None
                msg = f"{fg(GREY)}Restarted. Play the next book move.{RESET}"
            elif k == "h" and not done and nextmv:
                hint = {nextmv.from_square, nextmv.to_square}
                msg = f"{fg(th_secondary(self))}Hint: {op['moves'][ply]}{RESET}"
            elif k == "w" and not done and nextmv:  # auto-play this move
                board.push(nextmv); ply += 1; hint = None
                msg = f"{fg(GREY)}Played {op['moves'][ply-1]} for you.{RESET}"
            elif k in ("UP", "DOWN", "LEFT", "RIGHT", "j", "k", "l"):
                vr, vc = self.cursor_step(vr, vc, k)
            elif k == "ENTER" and not done:
                sq = cursor_sq(vr, vc, white_bottom, self)
                pc = board.piece_at(sq)
                if selected is None:
                    if pc and pc.color == board.turn:
                        selected, targets = sq, self.targets_for(board, sq)
                elif sq in targets:
                    mv = chess.Move(selected, sq)
                    if board.piece_at(selected).piece_type == chess.PAWN and chess.square_rank(sq) in (0, 7):
                        mv = chess.Move(selected, sq, promotion=self.pick_promotion())
                    if mv == nextmv:
                        board.push(mv); ply += 1; hint = None
                        msg = f"{fg(GREEN)}✓ {op['moves'][ply-1]}{RESET} {fg(GREY)}— book move.{RESET}"
                    else:
                        msg = f"{fg(RED)}✗ Not the book move here. {RESET}{fg(GREY)}h for hint.{RESET}"
                    selected, targets = None, set()
                elif pc and pc.color == board.turn:
                    selected, targets = sq, self.targets_for(board, sq)
                else:
                    selected, targets = None, set()
            elif k == "ENTER" and done:
                return

    def render_opening(self, board, white_bottom, cursor, selected, targets, hint, op, ply, msg, done):
        th = self.th
        self._fit()
        boardb = self.render_board(board, white_bottom, cursor, selected, targets, None, hint,
                                   self.check_square(board))
        w = self.panel_w
        panel = []
        panel.append(f"{fg(th['primary'])}{BOLD}{op['name']}{RESET} {fg(GREY)}{op['eco']}{RESET}")
        panel.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        # moves with progress
        seg = ""
        for i, m in enumerate(op["moves"]):
            if i % 2 == 0:
                seg += f"{fg(DGREY)}{i//2+1}.{RESET}"
            col = GREEN if i < ply else (th["accent"] if i == ply else GREY)
            seg += f"{fg(col)}{m}{RESET} "
        for wl in self.wrap_colored(seg, w - 2):
            panel.append(wl)
        panel.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        panel.append(f"{fg(th['primary'])}{BOLD}idea{RESET}")
        for wl in textwrap.wrap(op["idea"], w - 2):
            panel.append(f"{fg(GREY)}{wl}{RESET}")
        panel.append(f"{fg(DGREY)}" + "─" * (w - 2) + RESET)
        if done:
            panel.append(f"{fg(GREEN)}★ Line complete!{RESET}")
        for wl in textwrap.wrap(_ANSI_RE.sub('', msg), w - 2)[:2]:
            panel.append(clip(msg, w - 2) if wl == _ANSI_RE.sub('', msg) else f"{fg(GREY)}{wl}{RESET}")
        hints = self.help_line([("↑↓←→", "move"), ("↵", "play"),
                                ("h", "hint"), ("w", "auto"), ("r", "restart"),
                                ("g", self.piece_style), ("n", "back"), ("q", "menu")])
        self.draw(self.compose("opening trainer", boardb, None, panel, hints, white_bottom))
        self._overlay(board, targets)

    def wrap_colored(self, seg, w):
        # naive word wrap that respects ANSI by measuring visible length
        words = seg.split(" ")
        lines, cur, curw = [], "", 0
        for word in words:
            vl = vis_len(word)
            if curw + vl + (1 if cur else 0) > w and cur:
                lines.append(cur)
                cur, curw = word, vl
            else:
                cur = (cur + " " + word) if cur else word
                curw += vl + (1 if curw else 0)
        if cur:
            lines.append(cur)
        return lines

    # ───────────────────────── help ─────────────────────────
    def help_screen(self):
        th = self.th
        body = [
            f"{fg(th['primary'])}{BOLD}gambit{RESET} {fg(GREY)}— learn chess in the terminal{RESET}", "",
            f"{fg(WHITE)}Play vs Engine{RESET} {fg(GREY)}Full games at 8 strengths. After every move the{RESET}",
            f"{fg(GREY)}               coach rates it (best/inaccuracy/mistake/blunder){RESET}",
            f"{fg(GREY)}               and shows the better move you missed.{RESET}",
            f"{fg(WHITE)}Game Review{RESET}    {fg(GREY)}Replay your last game; each of your moves is graded{RESET}",
            f"{fg(GREY)}               and the stronger line you missed is shown.{RESET}",
            f"{fg(WHITE)}Analysis{RESET}       {fg(GREY)}Move freely, watch the live eval bar and the{RESET}",
            f"{fg(GREY)}               engine's best line update.{RESET}",
            f"{fg(WHITE)}Puzzles{RESET}        {fg(GREY)}Engine-verified tactics. Your move is graded live.{RESET}",
            f"{fg(WHITE)}Openings{RESET}       {fg(GREY)}Drill named opening lines move by move with the idea.{RESET}",
            "",
            f"{fg(th['primary'])}{BOLD}board controls{RESET}",
            f"{fg(th['secondary'])}mouse{RESET}         {fg(GREY)}click a piece then a square, or drag-and-drop{RESET}",
            f"{fg(th['secondary'])}↑↓←→ / hjkl{RESET} {fg(GREY)}move the cursor{RESET}",
            f"{fg(th['secondary'])}↵ enter{RESET}       {fg(GREY)}pick a piece, then its destination{RESET}",
            f"{fg(th['secondary'])}h{RESET}             {fg(GREY)}hint / best move    {fg(th['secondary'])}f{RESET} {fg(GREY)}flip board{RESET}",
            f"{fg(th['secondary'])}u{RESET}             {fg(GREY)}undo                {fg(th['secondary'])}t{RESET} {fg(GREY)}cycle theme{RESET}",
            f"{fg(th['secondary'])}g{RESET}             {fg(GREY)}piece set: sprites ▸ solid ▸ classic ▸ letters{RESET}",
            "",
            f"{fg(GREY)}{ITAL}sprites = graphic piece images (default); letters = any font.{RESET}",
            f"{fg(GREY)}{ITAL}Board auto-sizes to your window; bigger window = bigger board.{RESET}",
            f"{fg(GREY)}{ITAL}Engine: {self.path or 'NOT FOUND — install stockfish'}{RESET}",
        ]
        box = self.frame("help", body, 70)
        self.draw(self.center(box + ["", self.help_line([("↵", "back")])]))
        while True:
            k = self.wait_key()
            if k in ("ENTER", "q", "ESC"):
                return

    # ───────────────────────── main loop ─────────────────────────
    def run(self):
        self.enter()
        try:
            while self.running:
                eng_ok = bool(self.path)
                tag = "" if eng_ok else " (no engine)"
                played = " · review your last game" if getattr(self, "last_game", None) else ""
                items = [
                    ("Play vs Engine", "full game with live coaching" + tag),
                    ("Game Review", "grade your last game move-by-move" + tag + played),
                    ("Analysis Board", "free play + live eval bar" + tag),
                    ("Puzzles", "engine-verified tactics" + tag),
                    ("Opening Trainer", "drill named opening lines"),
                    ("Help", "controls & how it works"),
                    ("Quit", "leave gambit"),
                ]
                # show banner above menu via subtitle trick
                choice = self.main_menu(items)
                try:
                    if choice == 0:
                        self.play_mode()
                    elif choice == 1:
                        self.review_mode()
                    elif choice == 2:
                        self.analysis_mode()
                    elif choice == 3:
                        self.puzzle_mode()
                    elif choice == 4:
                        self.opening_mode()
                    elif choice == 5:
                        self.help_screen()
                    elif choice in (6, None):
                        break
                except _BackToMenu:
                    pass
                if getattr(self, "_goto_review", False):
                    self._goto_review = False
                    try:
                        self.review_mode()
                    except _BackToMenu:
                        pass
        finally:
            self.leave()

    def main_menu(self, items):
        self.clear_sprites()        # wipe any graphic pieces left by a board screen
        sel = 0
        while True:
            th = self.th
            body = self.banner() + [""]
            for i, (label, desc) in enumerate(items):
                if i == sel:
                    body.append(f"{fg(th['primary'])}▌{RESET}{bg(lerp(DARK, th['primary'], 0.20))}{fg(WHITE)}{BOLD} {label} {RESET}  {fg(GREY)}{ITAL}{desc}{RESET}")
                else:
                    body.append(f"  {fg(WHITE)}{label}{RESET}  {fg(DGREY)}{desc}{RESET}")
            box = self.frame(f"gambit · {th['name']}", body, 64)
            hint = self.help_line([("↑↓", "move"), ("↵", "open"),
                                   ("t", "theme"), ("q", "quit")])
            self.draw(self.center(box + ["", hint]))
            k = self.wait_key()
            if k in ("UP", "k"):
                sel = (sel - 1) % len(items)
            elif k in ("DOWN", "j"):
                sel = (sel + 1) % len(items)
            elif k == "ENTER":
                return sel
            elif k == "t":
                self.cycle_theme()
            elif k in ("q", "ESC", "CTRL_C"):
                return None


class _BackToMenu(Exception):
    pass


def cursor_sq(vr, vc, white_bottom, app):
    return app.vis_to_sq(vr, vc, white_bottom)


def th_secondary(app):
    return app.th["secondary"]


def main():
    app = Gambit()
    try:
        app.run()
    except KeyboardInterrupt:
        app.leave()
    except Exception:
        app.leave()
        raise


if __name__ == "__main__":
    main()
