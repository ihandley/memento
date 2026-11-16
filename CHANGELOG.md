# Changelog

## [1.1.0](https://github.com/ihandley/memento/compare/v1.0.0...v1.1.0) (2026-11-16)


### Features

* add check-engine to test the live extraction engine ([a56fb96](https://github.com/ihandley/memento/commit/a56fb96268345c04bf5cd384685d8de29d47cb2d))
* add check-engine to test the live extraction engine ([8f4bf9c](https://github.com/ihandley/memento/commit/8f4bf9cbcab64938ad598da0686ba563efdddfde)), closes [#40](https://github.com/ihandley/memento/issues/40)
* add memento digest of earlier-session injections ([8e99eff](https://github.com/ihandley/memento/commit/8e99effc083b995636f0bae047f8b2b3530a06d3))
* add memento digest of earlier-session injections ([#43](https://github.com/ihandley/memento/issues/43)) ([4e52396](https://github.com/ihandley/memento/commit/4e5239684305d3c43fabd23293b95ebda2a1b9f0))
* add memento eval to replay past prompts through retrieval and judge results ([#33](https://github.com/ihandley/memento/issues/33)) ([82e27a2](https://github.com/ihandley/memento/commit/82e27a2a3d1f9d2754c0b4c215352eda5c80a062))
* choose the extraction engine and which sources nightly imports ([124d7ff](https://github.com/ihandley/memento/commit/124d7ff3dbe1ab86d43701835935d81c5b67d127))
* chunk oversized sessions instead of blocking them ([594731d](https://github.com/ihandley/memento/commit/594731dbe4e0b41aed7ec47c67abdb44122c802e))
* chunk oversized sessions instead of blocking them ([#28](https://github.com/ihandley/memento/issues/28)) ([08ab27b](https://github.com/ihandley/memento/commit/08ab27b85cd963dbfea1e4a99a407178bda8521f))
* discover projects under configured roots and fold in worktrees ([#32](https://github.com/ihandley/memento/issues/32)) ([88cfdb2](https://github.com/ihandley/memento/commit/88cfdb2e448775dfa07c4547dce541c5dc867b9e))
* injection receipt ([#42](https://github.com/ihandley/memento/issues/42)) ([3f75487](https://github.com/ihandley/memento/commit/3f75487b5436b8c1f7cb4bbb3c23e0239026a392))
* make Cursor extractor timeout configurable ([#39](https://github.com/ihandley/memento/issues/39)) ([e3fee74](https://github.com/ihandley/memento/commit/e3fee7414111492363092f4123a2d266fd2b2d98)), closes [#38](https://github.com/ihandley/memento/issues/38)
* print a one-line receipt when the hook injects records ([#42](https://github.com/ihandley/memento/issues/42)) ([af19e89](https://github.com/ihandley/memento/commit/af19e89a385208dbef55aaec14f2342cde743be7))


### Bug Fixes

* drop the receipt log, remove bare except, cap receipt titles at 60 chars ([f3420bc](https://github.com/ihandley/memento/commit/f3420bc36247a1b0e722914da6c7464396f0badd))
* limit hook receipts to injected records, omit prompts, normalize titles, and tolerate write failures ([067792f](https://github.com/ihandley/memento/commit/067792f7f1d501ef092e0d6bcd57a0c977412c5f))
* scrub injection log on forget; digest only counts earlier records ([c5c1100](https://github.com/ihandley/memento/commit/c5c1100d75cdcf153231646eddc52b41ac10f549))
