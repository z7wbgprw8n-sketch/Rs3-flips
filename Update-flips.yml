name: update-flips
on:
  schedule:
    - cron: "*/10 * * * *"
  workflow_dispatch:
permissions:
  contents: write
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python flips.py
        env:
          DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
      - run: |
          git config user.name "flip-bot"
          git config user.email "flip-bot@users.noreply.github.com"
          git add docs
          git commit -m "update flips" || exit 0
          git push
