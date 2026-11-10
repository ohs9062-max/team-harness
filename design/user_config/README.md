# 전역 설정 원본과 사용자 override

`worker_suffix.md`와 `design/designs/operating_policy.md`는 버전 관리하는 공통 정책이다. `config.toml`은 전역 기본값 예시이며 사용자 provider·모델·키는 배포 시 보존한다. 공통 정책을 system_prompt 안에 다시 복사하지 않는다.

검증·commit·push 후 `th-sync.sh --apply`가 전역 명령·정책·스킬을 설치하고 워크트리를 안전하게 fast-forward한다. `th-sync.sh --status`는 설치 해시 차이와 반영되지 않은 워크트리를 표시한다. 배포 전에 변경 파일을 deployment-backups에 보관한다. 설치 경로와 적용 시점은 `design/designs/reliability-and-global-deployment.md`를 따른다.
