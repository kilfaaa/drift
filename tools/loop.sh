#!/usr/bin/env bash
# Дежурство бота: один запуск GitHub Actions живёт ~5,5 часа и каждые 30 минут (в :07 и :37 МСК)
# делает обычный шаг бота (auto). Расписание GitHub срабатывает раз в несколько часов, поэтому так надёжнее.
set -u
END=$(( $(date +%s) + ${LOOP_MINUTES:-330} * 60 ))
git config user.name "medvedovka-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

step() {
  git pull -q --rebase -X theirs origin main || { git rebase --abort 2>/dev/null; git reset -q --hard origin/main; }
  python bot/run.py --mode auto --who "" || echo "шаг бота упал"
  git add bot/state.json data/
  [ -f crew/stats.json ] && git add crew/stats.json
  if git diff --cached --quiet; then echo "без изменений"; return; fi
  git commit -qm "бот: прогноз, журнал, статистика"
  for i in 1 2 3 4; do
    if git pull -q --rebase -X theirs origin main && git push -q origin HEAD:main; then return; fi
    git rebase --abort 2>/dev/null || true
    sleep $((i * 5))
  done
  echo "не удалось сохранить, попробую в следующий раз"
}

while :; do
  echo "== шаг $(TZ=Europe/Moscow date '+%d.%m %H:%M')"
  step
  # до следующей отметки :07 или :37
  now=$(date +%s); m=$(( (now / 60) % 60 )); s=$(( now % 60 ))
  if [ $m -lt 7 ]; then wait=$(( (7 - m) * 60 - s ));
  elif [ $m -lt 37 ]; then wait=$(( (37 - m) * 60 - s ));
  else wait=$(( (67 - m) * 60 - s )); fi
  [ $(( now + wait )) -lt $END ] || break
  sleep $wait
done
echo "дежурство окончено"
