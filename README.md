# IR 피드백 외부 모듈 · CSV

버전 2026.10.27-csv.6. 기존 Monitoring CSV의 TemperaturePostRecoating을 읽어 IR 설정을 계산합니다. 이 버전은 온도계 비활성 상태를 나타내는 -1 값을 유효 온도로 처리하지 않고 제어 입력을 중단합니다. 정상 범위에 있는 오래된 센서값은 CSV만으로 구별할 수 없습니다.

압축을 새 폴더에 전체 해제하고 IRFeedback.exe를 실행하세요. Python을 별도 설치할 필요가 없으며 이 패키지에는 Python 3.14.5와 Tk 8.6.15가 포함되어 있습니다. 기존 BinderJet의 Sequence 화면에서 IR 값을 변경한 후 Save하는 방식으로 적용합니다. 저장한 값의 실제 히터 반영은 원본 장비 프로그램의 히터 시작 시점에 따릅니다.

이 버전은 실제 S3 장비의 센서, PLC 및 램프 출력으로 검증하지 않았습니다. 정상 범위의 오래된 센서값이 기록될 가능성이 남아 있으므로 실제 장비에서는 우선 조회 모드로 확인하고 자동 적용을 보류하세요.

자세한 조작 방법은 app/README.ko.md를 참고하세요.

층별 그래프는 최근 100개 층을 보여주며 전체보기와 CSV 저장에서 현재 연결 후 수신한 전체 층을 볼 수 있습니다. 이전 csv.5 ZIP에는 기본 알고리즘 설정이 빠져 있으므로 csv.6을 사용하세요.

[전체 ZIP 다운로드 (GitHub 릴리스)](https://github.com/rlaengus444-ui/IR_feedback/releases/tag/2026.10.27-csv.6) — 비공개 저장소에 접근할 수 있는 GitHub 계정으로 로그인해야 합니다.

[팀드라이브 ZIP](https://drive.google.com/file/d/1xoegIp6q66VMGi0ktI2ucNGRU4iUKtoj/view) — Google Drive가 악성코드 또는 스팸으로 분류하여 다운로드를 차단하고 있습니다.

SHA-256: `ff216aaca807a47b44de34f47194aff88f28dd0f08a9874f330cdd8c67be5e84`
