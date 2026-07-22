---
runtime_injection: false
source_of_truth: [../../agents/RTK.md, ../../scripts/rtk_pre_tool_use.py]
---

# RTK 운영 규칙

RTK는 shell 출력의 token 사용을 줄이는 CLI proxy다. 프로젝트의 Codex hook이 적용되는 환경에서는 shell 명령의 첫 executable로 `rtk`를 사용한다.

```powershell
rtk git status
rtk pytest -q
rtk gain
rtk gain --history
rtk proxy <executable> <arguments>
```

Native Windows에서 bare `bash`는 WSL relay로 갈 수 있으므로 사용하지 않는다. Git Bash가 필요하면 다음처럼 absolute executable을 RTK proxy로 직접 실행한다.

```text
rtk proxy "C:\Program Files\Git\bin\bash.exe" -lc "cd /c/project/repo && ./scripts/task.sh"
```

PowerShell `&`, `cmd /c`, direct `.sh`, `rtk run`을 Git Bash 앞에 두지 않는다. 일반 사용자 terminal에 hook이 없는 경우 이 문서는 그 terminal 전역을 강제로 바꾸지 않는다.

확인은 `rtk --version`, `rtk gain`, `Get-Command rtk`로 한다. 실행 hook의 기계 권위는 `agents/RTK.md`와 `scripts/rtk_pre_tool_use.py`다.
