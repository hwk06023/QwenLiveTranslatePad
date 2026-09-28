class PCM16KProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate;
    this.inputIndex = 0;
    this.previous = null;
    this.previousIndex = 0;
    this.nextOutputAt = 0;
    this.chunk = new Int16Array(320);
    this.chunkOffset = 0;
  }

  emitSample(value) {
    const clamped = Math.max(-1, Math.min(1, value));
    this.chunk[this.chunkOffset++] = clamped < 0
      ? Math.round(clamped * 32768)
      : Math.round(clamped * 32767);

    if (this.chunkOffset === this.chunk.length) {
      const packet = this.chunk.buffer;
      this.port.postMessage(packet, [packet]);
      this.chunk = new Int16Array(320);
      this.chunkOffset = 0;
    }
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel || channel.length === 0) return true;

    for (let i = 0; i < channel.length; i += 1) {
      const current = channel[i];
      const currentIndex = this.inputIndex++;

      if (this.previous === null) {
        this.previous = current;
        this.previousIndex = currentIndex;
        if (this.nextOutputAt === 0) {
          this.emitSample(current);
          this.nextOutputAt += this.ratio;
        }
        continue;
      }

      while (this.nextOutputAt <= currentIndex) {
        const span = currentIndex - this.previousIndex || 1;
        const t = (this.nextOutputAt - this.previousIndex) / span;
        const sample = this.previous + (current - this.previous) * t;
        this.emitSample(sample);
        this.nextOutputAt += this.ratio;
      }

      this.previous = current;
      this.previousIndex = currentIndex;
    }

    return true;
  }
}

registerProcessor('pcm-16k-processor', PCM16KProcessor);
