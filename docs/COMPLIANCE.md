# Terms of service notes

This is **not legal advice**. These notes summarise the parts of Google's terms that
shaped this design, as read on 2026-11-20 (Gemini API Additional Terms effective
2026-03-23). The terms change, so check the current text:

- Google APIs Terms of Service: https://developers.google.com/terms
- Gemini API Additional Terms of Service: https://ai.google.dev/gemini-api/terms
- Rate limits: https://ai.google.dev/gemini-api/docs/rate-limits

## 1. Rate limits: stay within them, never get around them

> "You agree to, and will not attempt to circumvent, such limitations documented with each API."
> (Google APIs ToS §2(d))

> "Rate limits are applied per project, not per API key." (Rate limits page)

How the design complies:

- One project and one API key. No rotating keys, projects or accounts.
- Each model is used only up to its own published limit. Switching to another model
  with free quota uses limits Google grants separately for each model. It doesn't go past any of them.
- After a 429, the proxy waits for at least the delay Google gives (or 60 s, or until
  the daily reset) before trying that model again. It never retries in a fast loop.
- Only documented endpoints are used (`/v1beta/openai/`), as §2(c) requires ("only access
  an API by the means described in the documentation").

Project wording: describe it as a "quota-aware router" or "automatic fallback". Avoid
"bypass", "unlimited" or "beat the rate limit".

## 2. Publishing the code vs. offering a service

> "you will not create an API Client that functions substantially the same as the APIs
> and offer it for use by third parties." (Google APIs ToS §4(a)(1))

> "Developer credentials may not be embedded in open source projects." (§4(b)(1))

- The repository is source code that each user runs **on their own machine, with their
  own key**. The author doesn't run it as a service for anyone else.
- Do **not** host a public instance, or share your instance or key with other people.
- No credentials in the repository, in examples or in git history.

**Open question.** Our reading is that publishing source code that each person runs
privately with their own key is not "offering for use by third parties" an API client
that "functions substantially the same as the APIs". The terms don't address this case
directly, and Google hasn't confirmed this reading. It is an interpretation, not a settled
answer. The proxy only forwards requests to Google and adds routing; it doesn't resell,
pool or share access.

## 2a. Available regions

> "You may only access the Services (or make API Clients available to users) within an
> available region." (Gemini API Additional Terms, Use Restrictions; regions listed at
> https://ai.google.dev/gemini-api/docs/available-regions)

- Each user is responsible for using the API only from an available region. The list
  changes; check it before you start.
- This project does nothing about region restrictions and gives no guidance on them.
- In the European Economic Area, Switzerland and the UK, only Paid Services may be used
  when making API Clients available to users (§3).

## 3. Free-tier data use

From the Gemini API Additional Terms, "Unpaid Services": Google uses submitted content
and responses "to provide, improve, and develop" its products, and "human reviewers may
read, annotate, and process your API input and output." Google advises: "Do not submit
sensitive, confidential, or personal information to the Unpaid Services."

The README must warn users not to send employer, client or confidential code through
a free-tier key.

Regional rule: "You may use only Paid Services when making API Clients available to
users in the European Economic Area, Switzerland, or the United Kingdom." Users in those
regions are responsible for their own compliance.

## 4. Trademarks

> "You will not make any statement regarding your use of an API which suggests
> partnership with, sponsorship by, or endorsement by Google without Google's prior
> written approval." (Google APIs ToS §6(c))

- Use "Gemini" only to describe what the tool works with (e.g. "... for the Gemini API"),
  not as the leading brand of the project name.
- No Google logos or Gemini artwork.
- The README carries this disclaimer: *"Not affiliated with, endorsed by, or sponsored by
  Google. Gemini is a trademark of Google LLC."*
