-- liquibase formatted sql
-- changeset xiaozhi:202605201230-1
ALTER TABLE `ai_agent`
  ADD COLUMN `tts_speaker_id` BIGINT NULL COMMENT 'OpenVoiceStream preset speaker ID' AFTER `tts_voice_id`;
