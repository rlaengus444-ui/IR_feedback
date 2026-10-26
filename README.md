# IR 피드백 외부 모듈

M1/S3 BinderJet의 원본 소스·실행 파일을 수정하지 않고, 기존 Sequences IR 입력·Save 경로로 피드백을 적용하는 Windows 64비트 외부 모듈입니다.

[Python 설치 없는 전체 배포본 ZIP (팀 드라이브)](https://drive.google.com/file/d/1FcFj4zA-jT9egEwRlR-fX4T3jSGbpzfK/view)

ZIP 전체를 풀고 IRFeedback.exe를 실행합니다. 기본 모드는 조회입니다. 실제 저장은 UI 자동 적용을 선택하고 연결해야 시작합니다. 원본에 추가 코드·API·설정은 필요하지 않습니다.

자동 적용 연결 준비에서 Sequences 설정 확인과 Process 표 준비를 한 번 진행합니다. 이후에는 다른 탭을 보거나 원본을 최소화해도 새 T_recoating 수신과 IR 피드백을 계속합니다. 제어 중 탭 전환·창 복원·포커스 이동은 하지 않습니다. 수동 설정 편집이나 표 갱신 실패 시 중단합니다. 스크롤 범위 없는 짧은 표는 숨김 갱신을 확인할 수 없어 Process 화면을 보이는 상태로 사용해야 합니다. 원본 UI가 응답하지 않는 경우에도 중단합니다.

자동 검사 75개, 제품 경로 통합 시나리오 30개, Excel 비교 8,085항목이 통과했습니다. 통합 시험은 독립된 시험 창과 합성 데이터를 사용합니다. 실제 장비의 램프 출력 변경과 장시간 운전 영향은 검증하지 않았습니다. 기존 S3 설정에 필요한 항목이 없으면 자동 적용을 거부합니다.

[사용법](app/README.ko.md) · [검증 기록](docs/VALIDATION.ko.md) · [팀 드라이브 자료](https://drive.google.com/drive/folders/1Fd_1vA4HLd2NpzHmsO7KMK1lrk5Dx3_5)

- app/: 외부 모듈, native 브리지, 알고리즘과 단위 검사
- validation/: 제품 어댑터·세션을 사용하는 시험 창, 30개 시나리오와 결과
- docs/: 검증과 무설치 실행 확인
- packaging/Launcher.cs: Windows 런처 소스
- dist/: 전체 배포 ZIP 위치와 체크섬

GitHub 소스 체크아웃에는 Python/Tk 런타임을 중복 저장하지 않았습니다. 설치 없이 사용하려면 위 전체 ZIP을 내려받으세요.

배포본 SHA-256: 38fc0693124e9e41e1ca1361f44bb1bb35ddd62b6489592358cd00069839eb1a
