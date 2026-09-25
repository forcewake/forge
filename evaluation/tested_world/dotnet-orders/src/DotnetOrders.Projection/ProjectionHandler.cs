using DotnetOrders.Common;

namespace DotnetOrders.Projection;

/// <summary>
/// The projection store: the durable side-effect recorder. The
/// business effect is COMMITTED HERE (the row lands) strictly BEFORE
/// the delivery is acknowledged — that ordering is the invariant the
/// redelivery arm attacks.
/// </summary>
public interface IProjectionStore
{
    /// <summary>Record the side effect for <paramref name="messageId"/>
    /// if (and only if) it was never recorded. Returns true when THIS
    /// call created the row, false when the effect already existed
    /// (a duplicate delivery or a redelivery after a crash).</summary>
    bool TryRecord(string messageId, string effect);

    /// <summary>The durable effects, in application order — the DB-side
    /// truth the harness asserts exactly-once from.</summary>
    IReadOnlyList<string> Effects();

    /// <summary>A durable snapshot of the store (survives the simulated
    /// consumer crash: a restarted consumer restores from it).</summary>
    string Snapshot();
}

/// <summary>The in-memory reference store — durable enough for the
/// crash simulation (the snapshot round-trips through a string, the
/// way a real store round-trips through its database).</summary>
public sealed class InMemoryProjectionStore : IProjectionStore
{
    private readonly List<string> _effects = [];
    private readonly HashSet<string> _recorded = [];

    public bool TryRecord(string messageId, string effect)
    {
        if (_recorded.Contains(messageId))
        {
            return false;
        }
        _recorded.Add(messageId);
        _effects.Add(effect);
        return true;
    }

    public IReadOnlyList<string> Effects() => _effects.AsReadOnly();

    public string Snapshot() => string.Join("\n", _effects);
}

/// <summary>One handled delivery's outcome.</summary>
public enum ProjectionOutcome
{
    /// <summary>This delivery applied the (single) business effect.</summary>
    Applied,
    /// <summary>The effect already existed — a duplicate or redelivery;
    /// the consumer acks WITHOUT re-applying.</summary>
    Duplicate,
}

/// <summary>
/// The consumer service: applies OrderCreated deliveries to the
/// projection store with EXACTLY-ONCE side effects, keyed on the
/// message id. Dialect v2 (accepts v1 payloads too — widening).
/// </summary>
public sealed class ProjectionHandler(IProjectionStore store)
{
    public ProjectionOutcome Handle(Delivery delivery)
    {
        if (!Contract.Accepts(delivery.Message))
        {
            throw new InvalidOperationException(
                $"message {delivery.MessageId} violates the frozen contract");
        }
        var applied = store.TryRecord(
            delivery.MessageId,
            $"projection:{delivery.Message.Id}:total={delivery.Message.Total}:region={delivery.Message.Region ?? "unknown"}");
        return applied ? ProjectionOutcome.Applied : ProjectionOutcome.Duplicate;
    }
}
