# IR 피드백 외부 모듈 · CSV

버전 2026.10.27-csv.2. 원본 소스·실행파일·설정에 추가 변경 없이 기존 Monitoring CSV의 TemperaturePostRecoating으로 계산합니다. Process 표 조회·접근성 객체·스크롤·Process 탭 전환은 사용하지 않습니다.

[Python 설치 없는 전체 ZIP (팀 드라이브)](https://drive.google.com/file/d/1FcFj4zA-jT9egEwRlR-fX4T3jSGbpzfK/view)

이전 외부 모듈을 닫고 새 ZIP을 새 폴더에 전체 해제한 뒤 IRFeedback.exe를 실행하세요. M1/S3 선택과 연결/연결 해제, 기본 조회·미리보기·명시적인 UI 자동 적용을 제공합니다. 연결 준비에서는 기존 Sequence 설정을 확인하며 Process로 이동하지 않습니다. 준비 후 다른 탭·최소화 상태에서도 CSV를 읽습니다.

CSV는 층 인쇄가 끝난 뒤 저장되므로 측정부터 저장까지의 지연은 남습니다. 약 0.25초 간격으로 완성된 새 행을 확인해 계산하고, 필요할 때 기존 IR 입력·Save로 한 번 적용합니다. 실측 램프 반영 시점은 원본에 의존합니다. 오래된 행·부분 행·중복·작업 변경·미저장 설정을 보호하고 모호한 Save는 재시도하지 않습니다.

목표 온도는 연결 중 변경할 수 있고 다음 새 층부터 반영합니다. Excel 수식 파일을 선택해 온도 입력·다음 IR 결과 및 현재 출력·목표 셀을 지정할 수 있습니다. 이 옵션에는 Microsoft Excel 데스크톱이 필요합니다. 연결 이후 층별 온도와 IR 설정 그래프도 표시합니다.

자동 검사, 합성 통합 시나리오, Excel 수치 비교와 이동된 경로의 무설치 실행 결과는 검증 기록에 있습니다. 표에 접근하면 실패하는 시험 창에서 Process 표 접근은 모든 시나리오 0회였습니다. 실장비 IR 출력 변경과 장시간 열 제어는 미검증입니다.

이전 2026.10.27-background.1은 사용 보류 버전입니다. 이번 변경은 외부 모듈의 데이터 입력 경로 교체이며, 원본 자체의 Process 화면 정지 원인을 해결했다는 의미는 아닙니다. 원본 UI가 응답하지 않으면 CSV 조회만 계속하고 IR 적용을 정지합니다.

[사용법](app/README.ko.md) · [검증 기록](docs/VALIDATION.ko.md) · [팀 드라이브 자료](https://drive.google.com/drive/folders/1Fd_1vA4HLd2NpzHmsO7KMK1lrk5Dx3_5)

- app/: CSV reader, native Sequence 어댑터, 알고리즘과 단위 검사
- validation/: 자체 CSV/시험 창과 제품 세션을 쓰는 18개 시나리오
- docs/: 검증과 무설치 실행 기록
- packaging/Launcher.cs: Windows 런처 소스
- dist/: 전체 ZIP 위치와 SHA-256

GitHub 소스 체크아웃에는 Python/Tk 런타임을 중복 저장하지 않았습니다. 설치 없이 사용하려면 전체 ZIP을 받으세요.

배포본 SHA-256: 960fd8926abc575808ab386274a7975edacd1d79c4ee9418a4a6d2c9e59f2d84
