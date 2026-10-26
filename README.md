# IR 피드백 외부 모듈 · CSV

버전 2026.10.27-csv.4. 기존 Monitoring CSV의 TemperaturePostRecoating을 읽어 IR 설정을 계산합니다. 이 버전은 온도계 비활성 상태를 나타내는 -1 값을 유효 온도로 처리하지 않고 제어 입력을 중단합니다. 정상 범위에 있는 오래된 센서값은 CSV만으로 구별할 수 없습니다.

압축을 새 폴더에 전체 해제하고 IRFeedback.exe를 실행하세요. Python을 별도 설치할 필요가 없으며 이 패키지에는 Python 3.14.5와 Tk 8.6.15가 포함되어 있습니다. 기존 BinderJet의 Sequence 화면에서 IR 값을 변경한 후 Save하는 방식으로 적용합니다. 저장한 값의 실제 히터 반영은 원본 장비 프로그램의 히터 시작 시점에 따릅니다.

이 버전은 실제 S3 장비의 센서, PLC 및 램프 출력으로 검증하지 않았습니다. 정상 범위의 오래된 센서값이 기록될 가능성이 남아 있으므로 실제 장비에서는 우선 조회 모드로 확인하고 자동 적용을 보류하세요.

자세한 조작 방법은 app/README.ko.md를 참고하세요.

[전체 ZIP 다운로드 (팀 드라이브)](https://drive.google.com/file/d/1aOazW0PM1LQN6KvjQQ5aH8GDXMeK9tlb/view)

SHA-256: `19091c07323be471b11ec47283aeab866638a23abb546f4d8e1a6aeecbe2bfe8`

**배포 상태:** Google Drive에서 이 ZIP의 다운로드가 악성코드 또는 스팸 분류로 차단됩니다. Drive 링크는 저장 위치이며 현재 다운로드 가능 여부가 확인되지 않았습니다.
