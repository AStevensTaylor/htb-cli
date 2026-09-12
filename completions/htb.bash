# htb-cli bash completion - source this file or drop it in
# /usr/share/bash-completion/completions/htb
_htb_completions() {
  local cur prev commands
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  commands="login logout whoami status search info start stop reset extend active ip \
todo submit shell exec vpn proxy challenge season config raw refresh completion"
  case "$prev" in
    htb) COMPREPLY=($(compgen -W "$commands" -- "$cur")); return;;
    vpn) COMPREPLY=($(compgen -W "up down status servers switch config" -- "$cur")); return;;
    proxy) COMPREPLY=($(compgen -W "up down status url" -- "$cur")); return;;
    challenge) COMPREPLY=($(compgen -W "list info start stop download" -- "$cur")); return;;
    config) COMPREPLY=($(compgen -W "list get set" -- "$cur")); return;;
    start|info|stop|reset|extend|todo|shell)
      local cache="${XDG_CACHE_HOME:-$HOME/.cache}/htb-cli/machines.json"
      if [ -f "$cache" ]; then
        COMPREPLY=($(compgen -W "$(grep -o '"name": "[^"]*"' "$cache" | cut -d'"' -f4)" -- "$cur"))
      fi
      return;;
  esac
  COMPREPLY=($(compgen -W "$commands --json --yes --debug --help" -- "$cur"))
}
complete -F _htb_completions htb

