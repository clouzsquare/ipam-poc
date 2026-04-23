# 저장소 가이드라인

## 🚀 프로젝트 구조 및 모듈 구성
이 저장소는 표준 풀스택 아키텍처를 따릅니다. 개발 시 다음 구조를 사용해 주세요:

- `backend/`: 핵심 API 로직 및 서버 측 구현이 포함됩니다. 모든 API 핸들러, 비즈니스 로직, 데이터베이스 상호작용 모듈이 이곳에 위치합니다.
- `frontend/`: UI 컴포넌트 및 상태 관리 로직을 포함한 클라이언트 측 애플리케이션이 있습니다.
- `sample-data/`: 개발 및 로컬 테스트 목적에 사용되는 예제 또는 모의(mock) 데이터가 저장됩니다.
- `INSTRUCTION.md` / `README.md`: 프로젝트에 대한 높은 수준의 설명 및 초기 설정 가이드가 포함되어 있습니다.

## 🛠️ 개발 및 빌드 명령어
환경 일관성을 보장하기 위해 루트 디렉토리에서 다음 명령어들을 사용하세요:
- **종속성 설치 (Install Dependencies)**: `npm install` (필요한 모든 패키지를 다운로드합니다).
- **로컬 실행 (Run Locally)**: `npm run dev` (개발을 위해 프론트엔드와 백엔드 서비스를 동시에 시작합니다).
- **빌드 (Build)**: `npm run build` (배포를 위한 최적화된 프로덕션 준비 완료 정적 자산을 생성합니다).

## 🎨 코딩 스타일 및 네이밍 규칙
- **들여쓰기 (Indentation)**: 기본 들여쓰기는 스페이스 2칸입니다.
- **네이밍 (Naming)**: React 컴포넌트에는 파스칼 케이스(PascalCase) (`PascalCaseComponent`)를 사용하고, 변수 및 함수에는 카멜 케이스(camelCase)를 사용합니다.
- **백엔드 (Backend)**: 비즈니스 로직과 데이터 접근 계층 간의 엄격한 모듈 분리를 유지해야 합니다.

## 🧪 테스트 가이드라인
프론트엔드 및 백엔드 양 계층에 걸쳐 유닛(Unit) 및 통합(Integration) 테스트 커버리지를 유지합니다.
- **프레임워크**: Jest (프론트엔드) & Pytest (백엔드).
- **테스트 실행**:
    - 프론트엔드: `npm test:unit` (`frontend/` 디렉토리 내의 유닛 테스트를 실행합니다).
    - 백엔드: `pytest backend/tests/` (핵심 API 서비스를 테스트합니다).
- **커버리지**: 최소 80% 이상의 테스트 커버리지를 유지하세요. 새로운 로직이 추가되면 즉시 테스트가 작성되어야 합니다.

## 📜 커밋 및 풀 리퀘스트 (PR) 가이드라인
- **커밋 메시지 (Commit Messages)**: Conventional Commits 사양을 따릅니다: `type(scope): description`.
    * 예시: `feat(user): add profile endpoint`, `fix(auth): resolve token expiry bug`.
- **풀 리퀘스트 (Pull Requests, PRs)**: 모든 PR은 다음과 같아야 합니다:
    1. 관련된 이슈에 링크되어야 합니다 (`Closes #XXX`).
    2. 변경 사항, 이유 및 검증 가능한 단계별 테스트 가이드가 포함된 종합적인 설명을 제공해야 합니다.

## 💡 아키텍처 개요
시스템은 RESTful API를 통해 통신합니다. 데이터 영속성(Data persistence)은 `backend/db/`에 위치한 서비스 계층에서 관리됩니다. 느슨한 결합(Loose coupling)과 유지보수성을 보장하기 위해 모듈 간 상호작용은 정의된 인터페이스를 통해서만 이루어져야 합니다.
