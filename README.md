# IR 피드백 외부 모듈

M1/S3 BinderJet 프로그램을 수정하지 않고 기존 Sequences의 IR 입력·Save 절차로 피드백을 적용하는 Windows 64비트 외부 모듈입니다.

## 실행판

[Python 설치 없는 전체 배포본 ZIP (팀 드라이브)](https://drive.google.com/file/d/1FcFj4zA-jT9egEwRlR-fX4T3jSGbpzfK/view)

ZIP 전체를 풀고 `IRFeedback.exe`를 실행합니다. 원본 프로그램에 추가 코드·API·설정은 필요하지 않습니다. 기본은 조회 모드이며 실제 저장은 `UI 자동 적용`을 선택하고 연결해야 시작합니다.

## 검증과 제한

자동 검사 74개, 통합 시험 13개, Excel 비교 8,085항목이 통과했습니다. 실제 장비의 IR 출력을 변경하는 시험은 하지 않았습니다. UI 처리 지연, 센서 정상 수신 여부, 실제 램프 출력과 장시간 운전 영향은 별도 검증이 필요합니다. 현재 보관된 S3 설정에 누락 항목이 있으면 자동 적용을 차단합니다.

[배포 및 검증 자료 폴더](https://drive.google.com/drive/folders/1Fd_1vA4HLd2NpzHmsO7KMK1lrk5Dx3_5)

배포본 SHA-256: `d60494098dab16bff9554caa4445db1aa960a285c715ffbacd87b959d7a8db95`
