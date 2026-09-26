"""
Worked explanations ("solutions") for questions.

A question's `explanation` is one string of Markdown + LaTeX: steps as a
numbered list, inline math in $…$, display math in $$…$$, lines separated by
"\\n". It is its own field — separate from `validation.rationale`, which
belongs to whoever last validated the question and gets overwritten by the
next AI or human verdict.

The browser renders it (common_ui.py `renderExplanationHTML` + KaTeX). This
module covers the two places without a browser:

  - `to_pdf_paragraphs()` — reportlab mini-markup, one entry per line, for
    the PDF answer keys. reportlab can't typeset LaTeX, so math goes
    through `latex_to_text()`, a readable plain-text approximation
    (fractions as a/b, powers as superscripts, Greek letters, ×, √).
  - `for_markdown()` — the text for markdown exports, which already are
    markdown, so it passes through nearly unchanged.
"""
from __future__ import annotations

import html
import re

MAX_LEN = 20_000

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ",
    "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "approx": "≈", "neq": "≠",
    "ne": "≠", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "to": "→", "rightarrow": "→",
    "leftarrow": "←", "Rightarrow": "⇒", "infty": "∞", "propto": "∝", "degree": "°",
    "circ": "°", "partial": "∂", "nabla": "∇", "sum": "Σ", "int": "∫", "sim": "~",
    "ldots": "…", "cdots": "…", "dots": "…", "quad": "  ", "qquad": "    ", "angle": "∠",
    "perp": "⊥", "parallel": "∥", "hbar": "ħ", "ell": "ℓ", "prime": "′",
}
_SUP = str.maketrans("0123456789+-−=()niax", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱᵃˣ")
_SUB = str.maketrans("0123456789+-−=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ")


def clean(text) -> str:
    """Normalise newlines, trim, cap length. "" for anything empty."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return s[:MAX_LEN]


def _group(s: str, i: int) -> tuple[str, int]:
    """Read one TeX argument at s[i]: a {braced} group or a single token."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] == "{":
        depth, j = 0, i
        while j < len(s):
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0:
                    return s[i + 1:j], j + 1
            j += 1
        return s[i + 1:], len(s)
    if s[i] == "\\":
        m = re.match(r"\\[A-Za-z]+", s[i:])
        if m:
            return m.group(0), i + len(m.group(0))
    return s[i], i + 1


def _to_script(body: str, table, marker: str) -> str:
    """Unicode super/subscript when every character has one, else ^(…)/_(…)."""
    flat = latex_to_text(body)
    if flat == "°":                                   # 30^\circ
        return flat
    converted = flat.translate(table)
    if all(c != o or c in " " for c, o in zip(converted, flat)) or not flat:
        return converted
    if re.fullmatch(r"\w+", flat):                    # R_{eq} -> R_eq
        return f"{marker}{flat}"
    return f"{marker}({flat})"


def latex_to_text(tex: str) -> str:
    """Readable plain-text rendering of a LaTeX math fragment."""
    s = str(tex or "")
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)|\\(.)", s[i:])
            if not m:
                i += 1
                continue
            name, other = m.group(1), m.group(2)
            i += len(m.group(0))
            if other is not None:                     # \, \; \  \{ \% …
                out.append({",": " ", ";": " ", ":": " ", "!": "", " ": " "}.get(other, other))
                continue
            if name in ("frac", "dfrac", "tfrac"):
                num, i = _group(s, i)
                den, i = _group(s, i)
                n, d = latex_to_text(num), latex_to_text(den)
                n = n if re.fullmatch(r"[\w.]+", n) else f"({n})"
                d = d if re.fullmatch(r"[\w.]+", d) else f"({d})"
                out.append(f"{n}/{d}")
            elif name == "sqrt":
                idx = ""
                if i < len(s) and s[i] == "[":
                    j = s.find("]", i)
                    idx, i = s[i + 1:j], j + 1
                body, i = _group(s, i)
                b = latex_to_text(body)
                root = {"3": "∛", "4": "∜"}.get(idx, "√")
                out.append(root + (b if re.fullmatch(r"[\w.]+", b) else f"({b})"))
            elif name in ("text", "mathrm", "textrm", "mathit", "mathbf", "textbf", "operatorname",
                          "mbox", "mathsf", "vec", "hat", "bar", "overline", "boldsymbol"):
                body, i = _group(s, i)
                out.append(latex_to_text(body) if name not in ("text", "mbox", "textrm") else body)
            elif name in ("left", "right", "big", "Big", "bigg", "Bigg", "displaystyle", "limits"):
                continue
            elif name in _GREEK or name in _SYMBOLS:
                out.append(_GREEK.get(name) or _SYMBOLS[name])
                # TeX swallows the space after a command word: "\Delta V" is ΔV.
                if name in _GREEK and s[i:i + 1] == " " and s[i + 1:i + 2].isalnum():
                    i += 1
            elif name in ("sin", "cos", "tan", "log", "ln", "exp", "sec", "csc", "cot", "max", "min"):
                out.append(name)
            else:
                out.append(name)
        elif c in "^_":
            body, i = _group(s, i + 1)
            out.append(_to_script(body, _SUP if c == "^" else _SUB, c))
        elif c in "{}":
            i += 1
        elif c == "~":
            out.append(" ")
            i += 1
        else:
            out.append(c)
            i += 1
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()


_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)


def _inline_markup(line: str) -> str:
    """One line of markdown+math → reportlab paragraph markup (escaped)."""
    parts: list[str] = []
    pos = 0
    for m in _MATH_RE.finditer(line):
        parts.append(_md_inline(html.escape(line[pos:m.start()], quote=False)))
        parts.append(f"<i>{html.escape(latex_to_text(m.group(1) or m.group(2)), quote=False)}</i>")
        pos = m.end()
    parts.append(_md_inline(html.escape(line[pos:], quote=False)))
    return "".join(parts)


def _md_inline(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", s)
    return s


def to_pdf_paragraphs(text: str) -> list[tuple[str, str]]:
    """[(kind, markup)] where kind is "para", "item" (list item, markup
    already carries its number/bullet) or "math" (a display equation on its
    own line). Blank lines are dropped; the caller spaces paragraphs."""
    out: list[tuple[str, str]] = []
    text = clean(text)
    # Display math may span lines; flatten it onto one so it stays one block.
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: "$$" + " ".join(m.group(1).split()) + "$$", text, flags=re.S)
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)[.)]\s+(.*)$", line)
        b = re.match(r"^[-*•]\s+(.*)$", line)
        if re.fullmatch(r"\$\$(.+)\$\$", line):
            out.append(("math", f"<i>{html.escape(latex_to_text(line[2:-2]), quote=False)}</i>"))
        elif m:
            out.append(("item", f"{m.group(1)}. {_inline_markup(m.group(2))}"))
        elif b:
            out.append(("item", f"• {_inline_markup(b.group(1))}"))
        else:
            out.append(("para", _inline_markup(re.sub(r"^#+\s*", "", line))))
    return out


def for_markdown(text: str) -> str:
    """Markdown exports: the explanation is already markdown."""
    return clean(text)
