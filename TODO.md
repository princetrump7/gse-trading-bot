# GSE Trading Bot 24/7 GitHub Deployment
Current Working Directory: c:/Users/princ/OneDrive/Desktop/trials

## Steps (approved plan breakdown)

### 1. GitHub CLI Auth (interactive - user completes)
- Run `gh auth login` (logs into github.com/princetrump7).

### 2. Git Init & Commit
- `git init`
- `git add gse_trading_bot.py debug-5ad608.log`
- `git commit -m "Initial GSE trading bot"`

### 3. Create Repo & Push
- `gh repo create gse-trading-bot --public --push --source=. --remote=origin`

### 4. Edit Script for GitHub Actions ✓
- Added `if os.getenv('GITHUB_ACTIONS') != 'true':` around startup msg.

### 5. Create Workflow ✓
- `.github/workflows/gse-bot.yml` created (runs every 10min or manual, nohup python, 6h max).

### 6. Push Changes

### 6. Push Changes
- `git add .`
- `git commit -m "Add GitHub Actions workflow"`
- `git push`

### 7. Run Bot
- Go to repo Actions tab, run 'gse-bot' workflow manually.

### 8. Monitor
- View live logs in Actions run.

✓ TODO.md created. Next: auth & git setup.
