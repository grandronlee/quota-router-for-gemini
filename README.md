# quota-router-for-gemini

A local, OpenAI-compatible router that sends each request to the best Gemini API model
that still has free-tier quota. It switches models automatically when one hits its rate
limit, and stays within Google's published limits for your own single project and key.

**Status: design phase. Nothing is implemented yet.**

- [Design](docs/DESIGN.md)
- [Implementation plan](docs/PLAN.md)
- [Terms of service notes](docs/COMPLIANCE.md)
- License: [MIT](LICENSE)

## Important

- You need your own Gemini API key. Run it only for yourself; don't host it for others.
- On the free tier, Google may use your prompts and responses to improve its products,
  and human reviewers may read them. Don't send confidential code or personal data.
- This doesn't raise or get around any limit. It uses the separate quota each model already has.

Not affiliated with, endorsed by, or sponsored by Google. Gemini is a trademark of Google LLC.
