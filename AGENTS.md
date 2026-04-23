## Coding conventions

### shell scripts

When using shellscripts, avoid setting traps on local variables using a single quote:

```bash
local file=foo
trap 'rm -f "$file"' EXIT
```

Instead invert the quotes:

```bash
trap "rm -f '$file'" EXIT
```

#### Templates

When used in templates that write bash code, like the inline scripts in gitlab jobs, prefer single quotes over double quotes when there is no bash expansion expected:

```yaml
"$[[ inputs.job_name ]]":
  stage: $[[ inputs.job_stage ]]
  image: $[[ inputs.image_repository ]]:$[[ inputs.image_tag ]]
  rules: $[[ inputs.job_rules ]]
  script:
    - |
      set -euo pipefail
      export RUNTIME_VARIABLE="${ORISUN_GITLAB_BASE_URL:-${CI_SERVER_URL}}"
      export TEMPLTATE_TIME_CONST='$[[ inputs.template_const ]]'
      if [ -n '$[[ inputs.template_cond ]]' ]; then
        export TEMPLATE_TIME_VARIABLE='$[[ inputs.template_val ]]'
      fi
```

### python

When writing python code, please use the best practices as recommended by the python community.

Here are the core best practices to follow when writing Python:

#### 0. Testing

Always use `uvx pyright {folder}` to scan projects after implementing changes.

#### 1. Follow PEP 8 (The Style Guide)

PEP 8 is the official style guide for Python code. Adhering to it ensures your code is readable to any other Python developer.

* **Naming Conventions:** Use `snake_case` for variables and functions, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.
* **Indentation:** Use 4 spaces per indentation level (never mix spaces and tabs).
* **Line Length:** Limit all lines to a maximum of 79 characters to keep code easily readable side-by-side.
* **Imports:** Keep imports at the top of the file, grouped by standard library, third-party, and local application imports.

#### 2. Embrace "Pythonic" Idioms

Python has unique features that allow you to write elegant and concise code.

* **Context Managers (`with` statement):** Always use context managers for managing resources like file streams or database connections. It ensures they are properly closed even if an error occurs.

```python
### Good
with open('file.txt', 'r') as file:
    data = file.read()

```

* **Comprehensions:** Use list, dictionary, and set comprehensions for simple loops. They are faster and more readable.

```python
### Good
squares = [x**2 for x in range(10)]

```

* **Iterating:** Use `enumerate()` when you need the index, and `zip()` to iterate over multiple sequences simultaneously. Avoid using `range(len(sequence))`.

#### 3. Use Type Hinting

While Python is dynamically typed, introducing type hints (standardized in Python 3.5+) drastically improves code clarity, helps IDEs catch errors before runtime, and serves as excellent documentation.

```python
def calculate_discount(price: float, discount_rate: float) -> float:
    return price * (1 - discount_rate)

```

#### 4. Normalize External Data At Ingestion Boundaries

When working with external APIs, files, or user input, normalize and validate
data at the ingestion boundary so the rest of the code can rely on stronger
types and fewer defensive checks.

* Prefer adapter-local normalization helpers over repeating defensive
  `getattr(..., default)` and fallback coercions throughout business logic.
* Prefer explicit coercion policies. Do not silently blanket-cast values with
  `str()` or `int()` unless accepting mixed scalar types is an intentional part
  of the interface.
* Keep domain models stricter than wire formats. Optionality and alternate
  shapes should usually be resolved before data reaches the main application
  logic.

#### 5. Prefer Expressions For Simple Selection And Transformation

Use Python's expression-oriented constructs when they express the intent more
clearly than imperative control flow.

* Prefer comprehensions for simple filtering and projection.
* Prefer `next(..., default)` for straightforward single-item selection.
* Prefer standard library idioms such as `dict.fromkeys()` when they directly
  encode the operation.
* Keep imperative loops for orchestration, mutation, logging, warnings, and
  multi-step branching.
* If the expression is harder to read than the loop, use the loop.

#### 6. Isolate Your Environments

Never install project dependencies globally on your machine.

* **Virtual Environments:** Always create a virtual environment (using `venv`, `virtualenv`, or tools like `Poetry` or `Conda`) for each project.
* **Dependency Management:** Track your project's requirements using a `requirements.txt` file or a `pyproject.toml` file so others can easily recreate your environment.

#### 7. Document Effectively

Code tells you *how*, but documentation tells you *why*.

* **Docstrings:** Write descriptive docstrings for all public modules, functions, classes, and methods. Use a standard format like Google, NumPy, or Sphinx.
* **Comments:** Use inline comments sparingly. If your code is too complex to understand without comments, it might need to be refactored or renamed for clarity.

#### 8. Database Access

Machine-managed persistent state lives in `buzz.sqlite`.

* Open SQLite state through `buzz.core.db.connect()` and run
  `buzz.core.db.apply_migrations()` before use.
* Prefer repository helpers in `buzz.core.db` over ad-hoc SQL in business
  logic modules.
* Keep user-edited configuration in YAML/JSON files; use SQLite for machine
  state only.

#### Personal preferences

Less is more: a cleaner, simpler approach over lots of features and "production ready" code. The idea is to start simple, see what needs improvement and then do it in a second run.
