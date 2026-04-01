# Contributing to Ratatoskr

Thank you for your interest in contributing!

## Development Setup

1. Clone the repository
2. Install dependencies: `uv sync --group dev`
3. Run tests: `uv run pytest tests/ -v`
4. Run linter: `uv run ruff check api_agent/`

## Code Style

- Follow PEP 8 (enforced by Ruff)
- Use type hints where possible
- Write tests for new features
- Keep functions focused and small

## Testing

- Write unit tests for new code
- Ensure all tests pass before submitting PR
- Aim for >80% coverage on new code

## Pull Request Process

1. Fork the repo and create a feature branch
2. Make your changes with clear commit messages
3. Update documentation if needed
4. Ensure tests pass and linting is clean
5. Submit PR with description of changes

## Reporting Issues

- Use GitHub Issues
- Provide clear reproduction steps
- Include environment details (Python version, OS, etc.)
- Attach relevant logs or error messages

## Questions?

Open a GitHub Discussion or Issue.
