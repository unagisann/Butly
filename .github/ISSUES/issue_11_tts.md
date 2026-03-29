---
title: "TTS 統合（音声合成）"
labels:
  - feature
  - multimedia
assignees:
  - unagisann
---

## 概要

**Priority**: Low（RP 対応時に優先度上昇）

SillyTavern は ElevenLabs / XTTS / Silero / AllTalk / システム TTS 等を統合。
Backyard AI もボイスチャット対応。Butly には音声入出力がない。

Raspi 5 でローカル TTS はリソース的に厳しいため、
外部 API or ネットワーク上の別マシンへの委譲が現実的。

## 検討すべき方式

- **Piper TTS**（ローカル、軽量、Raspi で動作報告あり）
- **XTTS v2**（ローカル、高品質だが GPU 推奨）
- **ElevenLabs / OpenAI TTS**（クラウド API）
- **VOICEVOX**（ローカル、日本語に強い、ただし Raspi 対応は要検証）

## タスク

- [ ] TTS プロバイダ抽象化設計（BaseTTSProvider）
- [ ] 最小 MVP: OpenAI TTS API or Piper での実装
- [ ] AI 応答テキストの音声再生（Streamlit `st.audio` で出力）
- [ ] config.json に TTS 設定セクション追加
- [ ] UI: TTS ON/OFF トグルと音声プロバイダ選択

## 関連

- 表情・スプライト issue と連携し、口パク同期等も将来検討
