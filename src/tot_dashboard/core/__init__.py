"""UrbanGuard 제품 기반 계층 — 인증·권한·영속화·감사추적.

기존 도메인 패키지(flood / crowd / road / traffic_weather)가 "무엇을 탐지하는가"를
담당한다면, 이 패키지는 "누가 그것을 다룰 수 있는가"를 담당한다.
설계 근거는 docs/ui_design_spec.md 4절(권한 매트릭스)이며, 그 표가 곧
:mod:`.roles` 의 PERMISSIONS 와 :mod:`.models` 의 테이블 구조가 된다.
"""
