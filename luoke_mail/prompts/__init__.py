"""The prompts, as files (top-level ADR 0019): each task has <task>.system.txt (the standing instructions),
<task>.user.txt (how one request's input is worded and in what order), <task>.retry.txt (what is said after an
unusable answer, when the task asks again) and <task>.schema.json (the answer's form); _shared.txt holds the pieces
more than one task uses. Code reads the files, prepares the values (cut, joined, dates written out), fills them in
and parses the answer: no prompt wording lives in the code.

A template is lines. `{{name}}` is a value; a section shows lines, or part of a line, depending on one:
  {{#name}}…{{/name}}   a list: once for each entry (an entry's own names first, then the outer ones; `{{.}}`
                        is an entry that is a plain value); anything else: once when it is true / not empty
  {{?name}}…{{/name}}   once when the value is true / not empty (a heading over a list)
  {{^name}}…{{/name}}   once when it is false / empty
  {{>name}}             the piece `name` of _shared.txt, defined there between {{$name}} and {{/name}}
A line that is only a section's opening or closing tag (or only a piece) is not a line of the result: the result is
the lines that are left, joined with a newline -- the way the code wrote them before, "\\n".join(lines). A file's
last newline is not part of it.

Strict, so a prompt never goes out with a hole in it: a name the template uses and the values do not have, or a
value given for a name the file never mentions (an entry of a list is a record: the template takes what it needs
from it), is an error (PromptError); so is a section that is not closed or a
`{{` that is not a tag. An enum is written once, in the schema: a prompt that lists the choices gets them from
`described()` (the schema's x-describe beside the enum; keys starting with x- are taken out before the schema is sent).
Standard library only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re

DIR = os.path.dirname(os.path.abspath(__file__))
KINDS = (".system.txt", ".user.txt", ".retry.txt", ".schema.json")
SHARED = "_shared.txt"
_TAG = re.compile(r"\{\{([#?^/>$]?)([a-z_]+|\.)\}\}")
OPEN = ("#", "?", "^")
_ALONE = re.compile(r"^[ \t]*\{\{([#?^/>])([a-z_]+)\}\}[ \t]*$")


class PromptError(Exception):
    pass


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return text[:-1] if text.endswith("\n") else text


def _names(text: str) -> set[str]:
    return {m.group(2) for m in _TAG.finditer(text) if m.group(1) in ("", "#", "?", "^") and m.group(2) != "."}


def _check(text: str, where: str) -> None:
    """Every {{ is a tag, and sections close in order."""
    if "{{" in _TAG.sub("", text) or "}}" in _TAG.sub("", text):
        raise PromptError(f"{where}：有认不出的占位符（只能是小写字母和下划线）")
    stack = []
    for m in _TAG.finditer(text):
        kind, name = m.groups()
        if kind in ("#", "?", "^", "$"):
            stack.append(name)
        elif kind == "/":
            if not stack or stack.pop() != name:
                raise PromptError(f"{where}：{{{{/{name}}}}} 没有对应的开头")
    if stack:
        raise PromptError(f"{where}：{{{{#{stack[-1]}}}}} 没有结尾")


def _pieces(text: str) -> dict[str, str]:
    """_shared.txt: {{$name}} … {{/name}}, each on a line of its own."""
    out, lines, i = {}, text.split("\n"), 0
    while i < len(lines):
        m = re.fullmatch(r"\{\{\$([a-z_]+)\}\}", lines[i].strip())
        if m:
            end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "{{/%s}}" % m.group(1)), None)
            if end is None:
                raise PromptError(f"{SHARED}：{m.group(1)} 没有结尾")
            out[m.group(1)] = "\n".join(lines[i + 1:end])
            i = end
        elif lines[i].strip():
            raise PromptError(f"{SHARED}：第 {i + 1} 行不在任何一段里")
        i += 1
    return out


class Template:
    def __init__(self, text: str, where: str, pieces: dict[str, str]):
        _check(text, where)
        self.where, self.pieces = where, pieces
        self.lines = self._expand(text.split("\n"), set())
        self.names = _names("\n".join(self.lines))

    def _expand(self, lines: list[str], seen: set[str]) -> list[str]:
        """Pieces put in place, before anything is filled: a piece is part of the file it is used in."""
        out = []
        for line in lines:
            for m in list(_TAG.finditer(line)):
                if m.group(1) == ">":
                    name = m.group(2)
                    if name not in self.pieces:
                        raise PromptError(f"{self.where}：{SHARED} 里没有 {name}")
                    if name in seen:
                        raise PromptError(f"{self.where}：{name} 引用了自己")
                    body = self._expand(self.pieces[name].split("\n"), seen | {name})
                    if _ALONE.match(line):
                        out += body
                        break
                    if len(body) > 1:
                        raise PromptError(f"{self.where}：多行的 {name} 要单独占一行")
                    line = line.replace(m.group(0), body[0] if body else "")
            else:
                out.append(line)
        return out

    def render(self, values: dict) -> str:
        extra = sorted(set(values) - self.names)
        if extra:
            raise PromptError(f"{self.where}：文件里没有 {extra}")
        return "\n".join(self._lines(self.lines, [values]))

    def _value(self, name: str, scopes: list):
        if name == ".":
            if isinstance(scopes[-1], dict):
                raise PromptError(f"{self.where}：{{{{.}}}} 只能用在一串值里")
            return scopes[-1]
        for s in reversed(scopes):
            if isinstance(s, dict) and name in s:
                return s[name]
        raise PromptError(f"{self.where}：没填 {name}")

    def _runs(self, kind: str, name: str, scopes: list) -> list[list]:
        """The scopes a section's body is shown in: none, once, or once per entry."""
        v = self._value(name, scopes)
        if kind == "^":
            return [] if v else [scopes]
        if not v:
            return []
        if kind == "#" and isinstance(v, list):
            return [scopes + [e] for e in v]
        return [scopes + [v]] if isinstance(v, dict) else [scopes]

    def _lines(self, lines: list[str], scopes: list) -> list[str]:
        out, i = [], 0
        while i < len(lines):
            m = _ALONE.match(lines[i])
            if m and m.group(1) in OPEN:
                kind, name, depth, j = m.group(1), m.group(2), 1, i + 1
                while j < len(lines):                    # the matching close: same name, same depth
                    n = _ALONE.match(lines[j])
                    if n and n.group(2) == name:
                        depth += 1 if n.group(1) in OPEN else -1 if n.group(1) == "/" else 0
                        if depth == 0:
                            break
                    j += 1
                if j == len(lines):
                    raise PromptError(f"{self.where}：{{{{{kind}{name}}}}} 要和结尾各占一行")
                for s in self._runs(kind, name, scopes):
                    out += self._lines(lines[i + 1:j], s)
                i = j + 1
                continue
            out.append(self._inline(lines[i], scopes))
            i += 1
        return out

    def _inline(self, text: str, scopes: list) -> str:
        out, pos = [], 0
        while True:
            m = _TAG.search(text, pos)
            if not m:
                return "".join(out) + text[pos:]
            out.append(text[pos:m.start()])
            kind, name = m.groups()
            if kind == "":
                v = self._value(name, scopes)
                if isinstance(v, bool) or not isinstance(v, (str, int)):
                    raise PromptError(f"{self.where}：{name} 要是文字或数字")
                out.append(str(v))
                pos = m.end()
                continue
            if kind not in OPEN:
                raise PromptError(f"{self.where}：一行中间不能有 {m.group(0)}")
            depth, k = 1, m.end()                        # the close on the same line
            while depth:
                n = _TAG.search(text, k)
                if not n:
                    raise PromptError(f"{self.where}：{m.group(0)} 的结尾要在同一行")
                if n.group(2) == name:
                    depth += 1 if n.group(1) in OPEN else -1 if n.group(1) == "/" else 0
                k = n.end()
            body = text[m.end():n.start()]
            out += [self._inline(body, s) for s in self._runs(kind, name, scopes)]
            pos = k


def files(task: str) -> list[str]:
    """The files a task is made of, sorted: what its fingerprint covers (exactly these names)."""
    have = set(os.listdir(DIR))
    return sorted(f for f in have if f in {task + k for k in KINDS} | {SHARED})


def _strip_x(schema, names: bool = False):
    """The schema without this repository's notes (keys starting with x-); `names`: the keys are property names."""
    if isinstance(schema, dict):
        return {k: _strip_x(v, names=k == "properties" and not names) for k, v in schema.items()
                if names or not k.startswith("x-")}
    if isinstance(schema, list):
        return [_strip_x(v) for v in schema]
    return schema


def _properties(schema, found: dict) -> dict:
    """Every property in the schema by name, however deep (an answer's items are a list of objects)."""
    if isinstance(schema, dict):
        for k, v in (schema.get("properties") or {}).items():
            found.setdefault(k, v)
        for v in schema.values():
            _properties(v, found)
    elif isinstance(schema, list):
        for v in schema:
            _properties(v, found)
    return found


def _shared() -> dict[str, str]:
    path = os.path.join(DIR, SHARED)
    return _pieces(_read(path)) if os.path.exists(path) else {}


class Task:
    def __init__(self, task: str):
        if not os.path.exists(os.path.join(DIR, task + ".system.txt")):
            raise PromptError(f"没有任务 {task} 的提示词")
        self.task = task
        pieces = _shared()
        base = os.path.join(DIR, task)
        self._system = Template(_read(base + ".system.txt"), task + ".system.txt", pieces)
        self._user = Template(_read(base + ".user.txt"), task + ".user.txt", pieces)
        self._retry = _read(base + ".retry.txt") if os.path.exists(base + ".retry.txt") else None
        if self._retry is not None:
            _check(self._retry, task + ".retry.txt")
            if _TAG.search(self._retry):
                raise PromptError(f"{task}.retry.txt：不填任何值")
        with open(base + ".schema.json", encoding="utf-8") as f:
            self.full_schema = json.load(f)
        self.props = _properties(self.full_schema, {})
        for name, spec in self.props.items():
            if "x-describe" in spec and "enum" in spec and [k for k, _ in spec["x-describe"]] != spec["enum"]:
                raise PromptError(f"{task}.schema.json：{name} 的 x-describe 和 enum 对不上（顺序也要一样）")
        h = hashlib.sha256()
        for f in files(task):
            with open(os.path.join(DIR, f), "rb") as fh:
                h.update(f.encode() + b"\0" + fh.read() + b"\0")
        self.sha256 = h.hexdigest()

    def system(self, **values) -> str:
        return self._system.render(values)

    def user(self, **values) -> str:
        return self._user.render(values)

    def messages(self, system: dict | None = None, **values) -> list[dict]:
        return [{"role": "system", "content": self.system(**(system or {}))}, {"role": "user", "content": self.user(**values)}]

    @property
    def retry(self) -> str:
        if self._retry is None:
            raise PromptError(f"{self.task} 没有 retry.txt")
        return self._retry

    def schema(self) -> dict:
        """What goes to the model (a fresh copy: a caller may narrow an enum)."""
        return _strip_x(copy.deepcopy(self.full_schema))

    def values(self, prop: str) -> list:
        """A property's choices: its enum, or, for a free text with suggested values, its x-describe."""
        spec = self.props.get(prop) or {}
        if "enum" in spec:
            return list(spec["enum"])
        if "x-describe" in spec:
            return [k for k, _ in spec["x-describe"]]
        raise PromptError(f"{self.task}.schema.json：{prop} 没有可选值")

    def described(self, prop: str, keep=None) -> list[dict]:
        """[{prop: value, "text": what it means, "more": not the last}] for a prompt that lists the choices;
        `keep`: only these values."""
        spec = self.props.get(prop) or {}
        if "x-describe" not in spec:
            raise PromptError(f"{self.task}.schema.json：{prop} 没有 x-describe")
        pairs = [(k, d) for k, d in spec["x-describe"] if keep is None or k in keep]
        return [{prop: k, "text": d, "more": i < len(pairs) - 1} for i, (k, d) in enumerate(pairs)]


_loaded: dict[str, Task] = {}
_alone: dict[str, Template] = {}


def load(task: str) -> Task:
    if task not in _loaded:
        _loaded[task] = Task(task)
    return _loaded[task]


def sha256(task: str) -> str:
    """The fingerprint of all the task's files (the evaluation record's prompt_sha256)."""
    return load(task).sha256


def piece(name: str, **values) -> str:
    """One piece of _shared.txt filled in on its own: for a request whose own wording is not in files yet."""
    if name not in _alone:
        pieces = _shared()
        if name not in pieces:
            raise PromptError(f"{SHARED} 里没有 {name}")
        _alone[name] = Template(pieces[name], f"{SHARED} 的 {name}", pieces)
    return _alone[name].render(values)
