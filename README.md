# IR 피드백 외부 모듈

M1/S3 BinderJet 프로그램을 수정하지 않고 기존 Sequences의 IR 입력·Save 절차로 피드백을 적용하는 Windows 64비트 외부 모듈입니다.

## 실행판

[Python 설치 없는 전체 배포본 ZIP (팀 드라이브)](https://drive.google.com/file/d/1FcFj4zA-jT9egEwRlR-fX4T3jSGbpzfK/view)

ZIP 전체를 풀고 `IRFeedback.exe`를 실행합니다. 원본 프로그램에 추가 코드·API·설정은 필요하지 않습니다. 기본은 조회 모드이며 실제 저장은 `UI 자동 적용`을 선택하고 연결해야 시작합니다.

## 검증과 제한

자동 검사 74개, 통합 시험 13개, Excel 비교 8,085항목이 통과했습니다. 실제 장비의 IR 출력을 변경하는 시험은 하지 않았습니다. UI 처리 지연, 센서 정상 수신 여부, 실제 램프 출력과 장시간 운전 영향은 별도 검증이 필요합니다. 현재 보관된 S3 설정에 누락 항목이 있으면 자동 적용을 차단합니다.

[배포 및 검증 자료 폴더](https://drive.google.com/drive/folders/1Fd_1vA4HLd2NpzHmsO7KMK1lrk5Dx3_5)

배포본 SHA-256: `d60494098dab16bff9554caa4445db1aa960a285c715ffbacd87b959d7a8db95`

## 저장소 구성

- `app/`: 외부 모듈, Windows native 브리지, 알고리즘과 단위 검사
- `validation/`: 실제 제품 어댑터를 쓰는 별도 시험 창과 13개 시나리오 결과
- `docs/`: 검증 결과와 무설치·한글/공백 경로 이동 시험 기록
- `packaging/Launcher.cs`: Windows 실행 파일 런처 소스
- `dist/`: 전체 배포 ZIP 위치와 SHA-256

GitHub의 소스 ZIP에는 Python/Tk 런타임을 중복 저장하지 않았습니다. 설치 없이 사용하려면 위의 **전체 배포본 ZIP**을 내려받으세요. 원본 BinderJet 저장소의 코드나 실행 파일을 교체하지 않습니다.
