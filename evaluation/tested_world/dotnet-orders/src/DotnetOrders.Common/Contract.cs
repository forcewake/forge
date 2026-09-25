namespace DotnetOrders.Common;

/// <summary>
/// The referee contract both services freeze against: the dialect the
/// pair speaks, the topic the producer publishes on, the shared
/// acceptance rule and the OLD (v1-only) compatibility gate used to
/// prove the negative old/new producer-consumer combination.
/// </summary>
public static class Contract
{
    public const string Dialect = OrderCreated.DialectV2;
    public const string Topic = "orders.events.order-created";

    /// <summary>The current (v2) acceptance rule: an id, a positive
    /// total, and any region shape (v1 or v2).</summary>
    public static bool Accepts(OrderCreated message) =>
        !string.IsNullOrWhiteSpace(message.Id) && message.Total > 0;
}

/// <summary>The OLD consumer's gate — a v1-only reader that treats the
/// v2 widening field as a contract violation. Kept in the shared
/// contract (not in the consumer) so the negative compatibility
/// combination is pinned by the frozen contract, not by consumer code
/// that could drift with the very change under verification.</summary>
public static class V1OnlyGate
{
    public const string RejectionReason =
        "dialect v1 does not admit the field 'region' (producer speaks v2)";

    public static bool Accepts(OrderCreated message, out string? reason)
    {
        if (message.Region is not null)
        {
            reason = RejectionReason;
            return false;
        }
        reason = null;
        return !string.IsNullOrWhiteSpace(message.Id) && message.Total > 0;
    }
}
