using DotnetOrders.Common;

namespace DotnetOrders.Common;

/// <summary>
/// One delivery of a message off the broker: the message id (the dedup
/// key), the broker's delivery sequence for THIS delivery, whether the
/// BROKER marked it a redelivery (an ack was never seen), and the
/// payload.
/// </summary>
public sealed record Delivery(string MessageId, long DeliveryTag, bool Redelivered, OrderCreated Message);
