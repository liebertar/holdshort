# sky-net

Follow [CONTRIBUTING.md](CONTRIBUTING.md): set-up, the checks to run before pushing, the branch and pull
request flow, and the commit format. The points that matter most:

- Branch from `dev` and open a pull request into `dev`. Only `dev` merges into `main`, with one review.
- Never `git push --force` or `git push --force-with-lease`.
- Commits: `type(scope): short description` in English, a body of `-` bullets in Korean, one logical change
  per commit, no co-author or tool trailers.
- Run `make lint`, `make test` and `node --test tests/test_map.mjs` before pushing.
- The runtime alone judges and commands; models never decide a clearance.
