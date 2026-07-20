# goldcomb tests

## Running the tests

You need Python >=3.10 and pytest installed. The project must be installed in editable mode:

```
/opt/homebrew/bin/python3.10 -m pip install -e .
/opt/homebrew/bin/python3.10 -m pip install pytest
/opt/homebrew/bin/python3.10 -m pytest
```

## Adding tests

- Place new tests in this directory as `test_*.py` files.
- Use `pytest` (function-based or class-based tests both work).
- For CLI tests, invoke the CLI with `subprocess.run()` as in the example test.
- Mock network or provider calls in deeper tests as needed to speed up and robustify tests.

## Coverage goals

- Aim to cover at least: provider add/remove logic, model switching, tool dispatch/errors, main agent execution paths.
- Example/test for project conventions and to aid contributors.
