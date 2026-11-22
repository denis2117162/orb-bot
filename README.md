# orb-bot

Paper-trading bot for a 5-minute opening range breakout on SPY, QQQ and IWM, traded as same-day at-the-money
options through Alpaca's paper API. Research and results live in a separate private repository.

- `orb_bot.py`: the bot (phases: dry, morning, afternoon, close).
- `.github/workflows/orb-bot.yml`: the schedule (times in UTC).

Paper only. Keys are read from GitHub encrypted secrets and never stored in this repository.
Not investment advice.
